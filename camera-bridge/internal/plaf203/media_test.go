package plaf203

import (
	"encoding/binary"
	"encoding/hex"
	"errors"
	"net"
	"testing"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/tutk"
)

// v303CapturedFirstH264Header is the decoded prefix of the first direct-LAN
// video datagram in camera-open.pcap at 2026-08-14T09:18:08.143877Z. The full
// payload is intentionally not kept in the test corpus.
const v303CapturedFirstH264Header = "04021b0a30041400080412008d3c00000c0000008d3cf50a9f26f8d80c000b000e000000000000140100000005000040440000000004631100000000010000000000000167"

func TestV303CapturedFirstH264HeaderHasExpectedFragmentShape(t *testing.T) {
	packet, err := hex.DecodeString(v303CapturedFirstH264Header)
	if err != nil {
		t.Fatal(err)
	}
	sessionID := [8]byte{0x8D, 0x3C, 0xF5, 0x0A, 0x9F, 0x26, 0xF8, 0xD8}
	if len(packet) < sessionHeaderLength+mediaHeaderLength || packet[0] != 0x04 || packet[1] != 0x02 || packet[2] != deviceSessionMagicVersion || string(packet[12:28]) != string(sessionID16(sessionID)) {
		t.Fatalf("unexpected V3.0.30 Session16 prefix: %x", packet)
	}
	inner := packet[sessionHeaderLength:]
	if inner[0] != 0x0C || inner[2] != loginCommandVersion || inner[16] != h264MainChannel || inner[20] != 68 || binary.LittleEndian.Uint16(inner[18:20]) != 0x4000 || binary.LittleEndian.Uint32(inner[28:32]) != 0 {
		t.Fatalf("unexpected V3.0.30 H.264 header: %x", inner)
	}
	if !containsH264NAL(inner[mediaHeaderLength:]) {
		t.Fatalf("first payload is not recognized as H.264: %x", inner[mediaHeaderLength:])
	}
}

func TestMediaReceiverAssemblesH264AndIgnoresDuplicateFragments(t *testing.T) {
	sessionID := [8]byte{1, 2, 3, 4, 5, 6, 7, 8}
	receiver := NewMediaReceiver()
	now := time.Unix(1_786_544_102, 0).UTC()
	first := testVideoFragment(sessionID, 0x4000, 2, 0, false, 7, []byte{0, 0, 0, 1, 0x67, 0x42})
	final := testVideoFragment(sessionID, 0x4001, 2, 1, true, 7, append([]byte{0, 0, 0, 1, 0x65, 0x88}, testMediaTrailer(42)...))

	for _, packet := range [][]byte{first, first} {
		frame, err := receiver.HandlePacket(packet, sessionID, now)
		if err != nil || frame != nil {
			t.Fatalf("first fragment frame=%+v err=%v", frame, err)
		}
	}
	frame, err := receiver.HandlePacket(final, sessionID, now)
	if err != nil {
		t.Fatal(err)
	}
	if frame == nil || frame.Codec != "h264" || !frame.Keyframe || frame.Timestamp != 42 {
		t.Fatalf("frame=%+v", frame)
	}
	if len(frame.Data) != 12 {
		t.Fatalf("frame data length=%d want=12", len(frame.Data))
	}
	stats := receiver.Snapshot()
	if stats.VideoCodec != "h264" || stats.FramesReceived != 1 || stats.BytesReceived != uint64(len(frame.Data)) {
		t.Fatalf("stats=%+v", stats)
	}
}

func TestMediaReceiverAcceptsV303TerminalFragmentFlags(t *testing.T) {
	sessionID := [8]byte{1, 2, 3, 4, 5, 6, 7, 8}
	receiver := NewMediaReceiver()
	now := time.Unix(1_786_544_102, 0).UTC()
	first := testVideoFragment(sessionID, 0x4000, 2, 0, false, 0, []byte{0, 0, 0, 1, 0x67})
	terminal := testVideoFragment(sessionID, 0x4001, 2, 16, true, 0, append([]byte{0, 0, 0, 1, 0x65}, testMediaTrailer(42)...))
	inner := terminal[sessionHeaderLength:]
	inner[1] = 0x05
	inner[17] = 0x01

	if frame, err := receiver.HandlePacket(first, sessionID, now); err != nil || frame != nil {
		t.Fatalf("first fragment frame=%+v err=%v", frame, err)
	}
	frame, err := receiver.HandlePacket(terminal, sessionID, now)
	if err != nil || frame == nil || !frame.Keyframe {
		t.Fatalf("terminal fragment frame=%+v err=%v", frame, err)
	}
}

func TestMediaReceiverRejectsUnexpectedSessionAndIncompleteFrame(t *testing.T) {
	sessionID := [8]byte{1, 2, 3, 4, 5, 6, 7, 8}
	receiver := NewMediaReceiver()
	now := time.Unix(1_786_544_102, 0).UTC()
	packet := testVideoFragment(sessionID, 0x4000, 2, 0, false, 7, []byte{0, 0, 0, 1, 0x67})
	wrongSession := sessionID
	wrongSession[0]++
	if _, err := receiver.HandlePacket(packet, wrongSession, now); !errors.Is(err, ErrUnexpectedPacket) {
		t.Fatalf("unexpected session error=%v", err)
	}
	if _, err := receiver.HandlePacket(packet, sessionID, now); err != nil {
		t.Fatal(err)
	}
	lateEnd := testVideoFragment(sessionID, 0x4001, 2, 1, true, 7, append([]byte{0, 0, 0, 1, 0x65}, testMediaTrailer(42)...))
	if _, err := receiver.HandlePacket(lateEnd, sessionID, now.Add(2*mediaAssemblyTimeout)); err == nil {
		t.Fatal("expired fragment assembly was accepted")
	}
}

func TestMediaReceiversKeepDeviceSessionsIsolated(t *testing.T) {
	firstID := [8]byte{1, 2, 3, 4, 5, 6, 7, 8}
	secondID := [8]byte{8, 7, 6, 5, 4, 3, 2, 1}
	now := time.Unix(1_786_544_102, 0).UTC()
	for _, testCase := range []struct {
		name      string
		receiver  *MediaReceiver
		sessionID [8]byte
	}{
		{name: "first", receiver: NewMediaReceiver(), sessionID: firstID},
		{name: "second", receiver: NewMediaReceiver(), sessionID: secondID},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			packet := testVideoFragment(testCase.sessionID, 0x4000, 1, 0, true, 0, append([]byte{0, 0, 0, 1, 0x61}, testMediaTrailer(1)...))
			frame, err := testCase.receiver.HandlePacket(packet, testCase.sessionID, now)
			if err != nil || frame == nil || frame.Keyframe {
				t.Fatalf("frame=%+v err=%v", frame, err)
			}
		})
	}
}

func TestSessionFansOutEveryFrameAndReplaysLatestKeyframe(t *testing.T) {
	session := &Session{}
	firstFrames := make([]*VideoFrame, 0)
	firstUnsubscribe := session.SubscribeFrames(func(frame *VideoFrame) {
		firstFrames = append(firstFrames, frame)
	})
	defer firstUnsubscribe()
	keyframe := &VideoFrame{Codec: "h264", Keyframe: true, Data: []byte{0, 0, 0, 1, 0x65}}
	session.publishFrame(keyframe)
	if len(firstFrames) != 1 || firstFrames[0] != keyframe {
		t.Fatalf("initial media fan-out=%+v", firstFrames)
	}
	lateFrames := make([]*VideoFrame, 0)
	lateUnsubscribe := session.SubscribeFrames(func(frame *VideoFrame) {
		lateFrames = append(lateFrames, frame)
	})
	defer lateUnsubscribe()
	if len(lateFrames) != 1 || !lateFrames[0].Keyframe || string(lateFrames[0].Data) != string(keyframe.Data) {
		t.Fatalf("latest keyframe replay=%+v", lateFrames)
	}
	session.publishFrame(&VideoFrame{Codec: "h264", Data: []byte{0, 0, 0, 1, 0x61}})
	if len(firstFrames) != 2 || len(lateFrames) != 2 {
		t.Fatalf("multi-consumer fan-out first=%d late=%d", len(firstFrames), len(lateFrames))
	}
}

func TestStreamingReceiverAcknowledgesCountersAndForwardsFrames(t *testing.T) {
	transport := &fakeTransport{}
	sessionID := [8]byte{0x59, 0x33, 0x60, 0x8D, 0xC8, 0xD1, 0xD8, 0x9E}
	peer := &net.UDPAddr{IP: net.ParseIP("192.0.2.25"), Port: 41135}
	transport.addResponse(tutk.TransCodePartial(nil, capturedSession25Counters(t, "session25_counters_device_decoded.hex")), peer)
	transport.addResponse(tutk.TransCodePartial(nil, testVideoFragment(sessionID, 0x4000, 1, 0, true, 0, append([]byte{0, 0, 0, 1, 0x65, 0x01}, testMediaTrailer(42)...))), peer)
	session := &Session{
		ID:        sessionID,
		Address:   peer,
		transport: transport,
		clock:     time.Now,
		sequence:  15,
		session25: newSession25State(),
		media:     NewMediaReceiver(),
	}
	frames := make(chan *VideoFrame, 1)
	unsubscribe := session.SubscribeFrames(func(frame *VideoFrame) {
		frames <- frame
	})
	defer unsubscribe()
	session.startMediaReceiver()
	defer session.cancelReceive()

	select {
	case frame := <-frames:
		if frame == nil || !frame.Keyframe {
			t.Fatalf("frame=%+v", frame)
		}
	case <-time.After(time.Second):
		t.Fatal("streaming receiver did not forward the media frame")
	}
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		transport.mu.Lock()
		sentCount := len(transport.sent)
		transport.mu.Unlock()
		if sentCount == 1 {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("streaming receiver did not acknowledge Session25 counters")
}
