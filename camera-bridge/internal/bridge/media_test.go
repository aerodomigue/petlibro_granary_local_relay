package bridge

import (
	"bufio"
	"net"
	"testing"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/core"
	"github.com/AlexxIT/go2rtc/pkg/rtsp"
	"github.com/AlexxIT/go2rtc/pkg/tcp"
	"github.com/aerodomigue/petlibro-camera-bridge/internal/plaf203"
	"github.com/pion/rtp"
)

func TestDeviceIDFromMediaPathOnlyAcceptsOneDeviceScopedRTSPPath(t *testing.T) {
	deviceID, ok := deviceIDFromMediaPath("/device/AF03040302A2B5B2CD60")
	if !ok || deviceID != "AF03040302A2B5B2CD60" {
		t.Fatalf("valid path result device=%q ok=%t", deviceID, ok)
	}
	for _, path := range []string{"/device/", "/streams/device", "/device/../../unsafe"} {
		if _, accepted := deviceIDFromMediaPath(path); accepted {
			t.Fatalf("unsafe media path accepted: %q", path)
		}
	}
}

func TestMediaPublisherConvertsVerifiedAnnexBFrameToAVCCWithoutReencoding(t *testing.T) {
	publisher := newMediaPublisher()
	packets := make(chan *rtp.Packet, 1)
	publisher.track.AppendChild(&core.Node{Input: func(packet *rtp.Packet) {
		packets <- packet
	}})
	frame := &plaf203.VideoFrame{
		Codec:    "h264",
		Keyframe: true,
		Data:     []byte{0, 0, 1, 0x67, 0x42, 0, 0x29},
	}
	publisher.publish(frame)

	if publisher.track.Packets != 1 {
		t.Fatalf("packets=%d want=1", publisher.track.Packets)
	}
	const expectedAVCCBytes = 8
	if publisher.track.Bytes != expectedAVCCBytes {
		t.Fatalf("bytes=%d want=%d", publisher.track.Bytes, expectedAVCCBytes)
	}
	packet := <-packets
	if packet.Version != 0 {
		t.Fatalf("packet version=%d want=0 for go2rtc AVCC packetization", packet.Version)
	}
	wantPayload := []byte{0, 0, 0, 4, 0x67, 0x42, 0, 0x29}
	if string(packet.Payload) != string(wantPayload) {
		t.Fatalf("packet payload=%x want=%x", packet.Payload, wantPayload)
	}
	publisher.mu.Lock()
	defer publisher.mu.Unlock()
	if publisher.latestFrame == nil || string(publisher.latestFrame.Data) != string(frame.Data) {
		t.Fatal("latest H264 keyframe was not retained for a later RTSP consumer")
	}
}

func TestRTSPConsumerRemainsOpenAfterPlay(t *testing.T) {
	registry := NewRegistryWithConnector(connectedConnector{})
	if _, err := registry.Upsert(testDeviceID, testUID, "192.0.2.10"); err != nil {
		t.Fatal(err)
	}
	serverConnection, clientConnection := net.Pipe()
	defer clientConnection.Close()
	server := &MediaServer{registry: registry}
	serverDone := make(chan struct{})
	go func() {
		server.handleConnection(serverConnection)
		close(serverDone)
	}()

	reader := bufio.NewReader(clientConnection)
	writeRTSPRequest(t, clientConnection, "OPTIONS rtsp://localhost/device/"+testDeviceID+" RTSP/1.0\r\nCSeq: 1\r\n\r\n")
	readRTSPResponse(t, reader)
	writeRTSPRequest(t, clientConnection, "DESCRIBE rtsp://localhost/device/"+testDeviceID+" RTSP/1.0\r\nCSeq: 2\r\nAccept: application/sdp\r\n\r\n")
	readRTSPResponse(t, reader)
	writeRTSPRequest(t, clientConnection, "SETUP rtsp://localhost/device/"+testDeviceID+"/trackID=0 RTSP/1.0\r\nCSeq: 3\r\nTransport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n\r\n")
	readRTSPResponse(t, reader)
	writeRTSPRequest(t, clientConnection, "PLAY rtsp://localhost/device/"+testDeviceID+" RTSP/1.0\r\nCSeq: 4\r\nSession: session\r\n\r\n")
	readRTSPResponse(t, reader)

	writeRTSPRequest(t, clientConnection, "OPTIONS rtsp://localhost/device/"+testDeviceID+" RTSP/1.0\r\nCSeq: 5\r\n\r\n")
	response := readRTSPResponse(t, reader)
	if response.StatusCode != 200 {
		t.Fatalf("post-PLAY request response=%q", response.Status)
	}
	_ = clientConnection.Close()
	select {
	case <-serverDone:
	case <-time.After(time.Second):
		t.Fatal("RTSP server did not close after client disconnect")
	}
}

func TestGo2RtcRTSPClientKeepsMediaSessionOpen(t *testing.T) {
	registry := NewRegistryWithConnector(connectedConnector{})
	if _, err := registry.Upsert(testDeviceID, testUID, "192.0.2.10"); err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	server := &MediaServer{registry: registry}
	serverDone := make(chan struct{})
	go func() {
		connection, acceptErr := listener.Accept()
		if acceptErr == nil {
			server.handleConnection(connection)
		}
		close(serverDone)
	}()

	client := rtsp.NewClient("rtsp://" + listener.Addr().String() + "/device/" + testDeviceID)
	client.Backchannel = true
	if err = client.Dial(); err != nil {
		t.Fatal(err)
	}
	defer client.Close()
	if err = client.Options(); err != nil {
		t.Fatal(err)
	}
	if err = client.Describe(); err != nil {
		t.Fatal(err)
	}
	if len(client.Medias) != 1 {
		t.Fatalf("media count=%d want=1", len(client.Medias))
	}
	if _, err = client.SetupMedia(client.Medias[0]); err != nil {
		t.Fatal(err)
	}
	if err = client.Play(); err != nil {
		t.Fatal(err)
	}
	clientDone := make(chan error, 1)
	go func() {
		clientDone <- client.Handle()
	}()
	select {
	case handleErr := <-clientDone:
		t.Fatalf("go2rtc RTSP client closed immediately after PLAY: %v", handleErr)
	case <-time.After(50 * time.Millisecond):
	}
	_ = client.Close()
	select {
	case <-serverDone:
	case <-time.After(time.Second):
		t.Fatal("RTSP server did not close after go2rtc client disconnect")
	}
}

func writeRTSPRequest(t *testing.T, connection net.Conn, request string) {
	t.Helper()
	if _, err := connection.Write([]byte(request)); err != nil {
		t.Fatalf("write RTSP request: %v", err)
	}
}

func readRTSPResponse(t *testing.T, reader *bufio.Reader) *tcp.Response {
	t.Helper()
	response, err := tcp.ReadResponse(reader)
	if err != nil {
		t.Fatalf("read RTSP response: %v", err)
	}
	return response
}
