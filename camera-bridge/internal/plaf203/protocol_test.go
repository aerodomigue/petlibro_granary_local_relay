package plaf203

import (
	"context"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"net"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/tutk"
)

const (
	protocolTestUID       = "PLAF2030000000000001"
	capturedResponseUID   = "FFKCANAN5PGESZYR111A"
	capturedResponseToken = "49acb16fd98e9d68"
)

func TestDecodeCapturedLANSearchResponse(t *testing.T) {
	wire := capturedLANSearchResponseWire(t)
	decoded := tutk.ReverseTransCodePartial(nil, wire)
	response, err := DecodeLANSearchResponse(decoded, capturedResponseUID)
	if err != nil {
		t.Fatal(err)
	}
	if len(wire) != lanSearchResponseLength || len(decoded) != lanSearchResponseLength {
		t.Fatalf("length wire=%d decoded=%d", len(wire), len(decoded))
	}
	if response.UID != capturedResponseUID || response.TailMarker != 0x03040704 || hex.EncodeToString(response.OpaqueToken[:]) != capturedResponseToken || response.ResponseFlags != 1 {
		t.Fatalf("unexpected response: %+v", response)
	}
	if binary.LittleEndian.Uint16(decoded[8:10]) != lanSearchResponseOpcode || binary.LittleEndian.Uint16(decoded[10:12]) != lanSearchResponseSubtype {
		t.Fatalf("header opcode=0x%04x subtype=0x%04x", binary.LittleEndian.Uint16(decoded[8:10]), binary.LittleEndian.Uint16(decoded[10:12]))
	}
}

func capturedLANSearchResponseWire(t *testing.T) []byte {
	t.Helper()
	wireHex, err := os.ReadFile("testdata/lan_search_response_wire.hex")
	if err != nil {
		t.Fatal(err)
	}
	wire, err := hex.DecodeString(strings.Join(strings.Fields(string(wireHex)), ""))
	if err != nil {
		t.Fatal(err)
	}
	return wire
}

func TestEncodeLANSearch3MatchesVerifiedLayout(t *testing.T) {
	nonce := [8]byte{1, 2, 3, 4, 5, 6, 7, 8}
	packet, err := EncodeLANSearch3(protocolTestUID, nonce)
	if err != nil {
		t.Fatal(err)
	}
	if len(packet) != lanSearchLength || packet[2] != clientMagicVersion || packet[3] != controlFlags {
		t.Fatalf("unexpected LAN_SEARCH3 header: %x", packet[:4])
	}
	if binary.LittleEndian.Uint16(packet[8:]) != lanSearchOpcode || string(packet[uidOffset:uidOffset+UIDLength]) != protocolTestUID {
		t.Fatalf("unexpected LAN_SEARCH3 identity: %x", packet)
	}
	if got := [8]byte(packet[lanSearchNonce : lanSearchNonce+8]); got != nonce {
		t.Fatalf("nonce=%x want=%x", got, nonce)
	}
	if packet[lanSearchPhase] != 1 {
		t.Fatalf("phase=%d", packet[lanSearchPhase])
	}
}

func TestEncodeLANSearch3PhaseTwoPreservesIdentityAndNonce(t *testing.T) {
	nonce := [8]byte{8, 7, 6, 5, 4, 3, 2, 1}
	packet, err := EncodeLANSearch3Phase(protocolTestUID, nonce, 2)
	if err != nil {
		t.Fatal(err)
	}
	if len(packet) != lanSearchLength || packet[lanSearchPhase] != 2 {
		t.Fatalf("unexpected phase-two packet: %x", packet)
	}
	if string(packet[uidOffset:uidOffset+UIDLength]) != protocolTestUID || [8]byte(packet[lanSearchNonce:lanSearchNonce+8]) != nonce {
		t.Fatalf("phase-two correlation mismatch: %x", packet)
	}
}

func TestDiscoverRequiresValidLANSearchResponse(t *testing.T) {
	nonce := [8]byte{9, 8, 7, 6, 5, 4, 3, 2}
	address := &net.UDPAddr{IP: net.ParseIP("192.0.2.20"), Port: 40238}
	transport := &fakeTransport{responses: []fakeDatagram{
		{
			packet:  tutk.TransCodePartial(nil, testLANSearchResponse("PLAF2030000000000002")),
			address: &net.UDPAddr{IP: net.ParseIP("192.0.2.21"), Port: 40238},
		},
		{
			packet:  tutk.TransCodePartial(nil, testLANSearchResponse(protocolTestUID)),
			address: address,
		},
	}}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	result, err := Discover(ctx, transport, protocolTestUID, nonce, []*net.UDPAddr{{IP: net.IPv4bcast, Port: LANPort}})
	if err != nil {
		t.Fatal(err)
	}
	if !result.IP.Equal(address.IP) || result.Port != address.Port {
		t.Fatalf("candidate=%v want=%v", result, address)
	}
	if len(transport.sent) != 1 {
		t.Fatalf("search sends=%d", len(transport.sent))
	}
}

func TestCompleteDirectDiscoverySendsPhaseTwoToDynamicPeer(t *testing.T) {
	nonce := [8]byte{9, 8, 7, 6, 5, 4, 3, 2}
	peer := &net.UDPAddr{IP: net.ParseIP("192.0.2.20"), Port: 41135}
	transport := &fakeTransport{}
	events := make([]Event, 0, 2)
	if err := CompleteDirectDiscovery(transport, peer, protocolTestUID, nonce, func(event Event) {
		events = append(events, event)
	}); err != nil {
		t.Fatal(err)
	}
	if len(transport.sent) != 1 {
		t.Fatalf("phase-two sends=%d", len(transport.sent))
	}
	phaseTwo := tutk.ReverseTransCodePartial(nil, transport.sent[0])
	if len(phaseTwo) != lanSearchLength || phaseTwo[lanSearchPhase] != 2 ||
		string(phaseTwo[uidOffset:uidOffset+UIDLength]) != protocolTestUID ||
		[8]byte(phaseTwo[lanSearchNonce:lanSearchNonce+8]) != nonce {
		t.Fatalf("unexpected phase-two packet: %x", phaseTwo)
	}
	if len(events) != 2 || events[0].Step != "phase_two_tx" || events[1].Step != "complete" ||
		events[1].Address == nil || !events[1].Address.IP.Equal(peer.IP) || events[1].Address.Port != peer.Port {
		t.Fatalf("events=%+v", events)
	}
}

func TestDiscoveryTimeoutIsBounded(t *testing.T) {
	transport := &fakeTransport{}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Millisecond)
	defer cancel()
	_, err := Discover(ctx, transport, protocolTestUID, [8]byte{}, []*net.UDPAddr{{IP: net.IPv4bcast, Port: LANPort}})
	if err == nil || !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("error=%v", err)
	}
}

func TestDiscoverUnicastAcceptsOnlyKnownIPWithDynamicSourcePort(t *testing.T) {
	knownIP := net.ParseIP("192.0.2.40")
	transport := &fakeTransport{}
	transport.onSend = func(_ []byte, address *net.UDPAddr) {
		if address == nil || !address.IP.Equal(knownIP) || address.Port != LANPort {
			t.Fatalf("unicast target=%v", address)
		}
		transport.addResponse(capturedLANSearchResponseWire(t), &net.UDPAddr{IP: net.ParseIP("192.0.2.41"), Port: 49152})
		transport.addResponse(tutk.TransCodePartial(nil, testLANSearchResponse("PLAF2030000000000002")), &net.UDPAddr{IP: knownIP, Port: 49153})
		transport.addResponse(capturedLANSearchResponseWire(t), &net.UDPAddr{IP: knownIP, Port: 41135})
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	result, err := DiscoverUnicast(ctx, transport, knownIP, capturedResponseUID, [8]byte{})
	if err != nil {
		t.Fatal(err)
	}
	if !result.IP.Equal(knownIP) || result.Port != 41135 || len(transport.sent) != 1 {
		t.Fatalf("result=%v sends=%d", result, len(transport.sent))
	}
}

func TestDirectDiscoveryUsesUnicastBeforeBroadcast(t *testing.T) {
	knownIP := net.ParseIP("192.0.2.40")
	transport := &fakeTransport{}
	connector := &DirectConnector{
		DiscoveryTargeter: func() ([]*net.UDPAddr, error) {
			t.Fatal("broadcast targeter must not run after successful unicast")
			return nil, nil
		},
	}
	transport.onSend = func(packet []byte, address *net.UDPAddr) {
		decoded := tutk.ReverseTransCodePartial(nil, packet)
		if len(decoded) != lanSearchLength {
			return
		}
		transport.addResponse(tutk.TransCodePartial(nil, testLANSearchResponse(protocolTestUID)), &net.UDPAddr{IP: knownIP, Port: 40238})
		if address == nil || !address.IP.Equal(knownIP) {
			t.Fatalf("unicast address=%v", address)
		}
	}
	result, mode, err := connector.discover(context.Background(), transport, protocolTestUID, [8]byte{}, knownIP, time.Second, nil)
	if err != nil || result == nil || mode != "unicast" || len(transport.sent) != 1 {
		t.Fatalf("result=%v mode=%s sends=%d err=%v", result, mode, len(transport.sent), err)
	}
}

func TestDirectDiscoveryFallsBackOnlyWhenEnabled(t *testing.T) {
	knownIP := net.ParseIP("192.0.2.40")
	broadcastIP := net.ParseIP("192.0.2.255")
	transport := &fakeTransport{}
	connector := &DirectConnector{
		BroadcastFallback: true,
		DiscoveryTargeter: func() ([]*net.UDPAddr, error) {
			return []*net.UDPAddr{{IP: broadcastIP, Port: LANPort}}, nil
		},
	}
	transport.onSend = func(packet []byte, address *net.UDPAddr) {
		decoded := tutk.ReverseTransCodePartial(nil, packet)
		if len(decoded) != lanSearchLength || address == nil || !address.IP.Equal(broadcastIP) {
			return
		}
		transport.addResponse(tutk.TransCodePartial(nil, testLANSearchResponse(protocolTestUID)), &net.UDPAddr{IP: knownIP, Port: 40238})
	}
	result, mode, err := connector.discover(context.Background(), transport, protocolTestUID, [8]byte{}, knownIP, 5*time.Millisecond, nil)
	if err != nil || result == nil || mode != "broadcast" || len(transport.sent) != 2 {
		t.Fatalf("result=%v mode=%s sends=%d err=%v", result, mode, len(transport.sent), err)
	}

	disabled := &DirectConnector{BroadcastFallback: false}
	_, _, err = disabled.discover(context.Background(), &fakeTransport{}, protocolTestUID, [8]byte{}, knownIP, 5*time.Millisecond, nil)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("fallback-disabled error=%v", err)
	}
}

func TestDirectDiscoveryUsesBroadcastWhenNoFeederIPIsKnown(t *testing.T) {
	broadcastIP := net.ParseIP("192.0.2.255")
	feederIP := net.ParseIP("192.0.2.40")
	transport := &fakeTransport{}
	connector := &DirectConnector{
		DiscoveryTargeter: func() ([]*net.UDPAddr, error) {
			return []*net.UDPAddr{{IP: broadcastIP, Port: LANPort}}, nil
		},
	}
	transport.onSend = func(packet []byte, address *net.UDPAddr) {
		if address == nil || !address.IP.Equal(broadcastIP) {
			t.Fatalf("broadcast address=%v", address)
		}
		transport.addResponse(tutk.TransCodePartial(nil, testLANSearchResponse(protocolTestUID)), &net.UDPAddr{IP: feederIP, Port: 40238})
	}
	result, mode, err := connector.discover(context.Background(), transport, protocolTestUID, [8]byte{}, nil, time.Second, nil)
	if err != nil || result == nil || mode != "broadcast" || !result.IP.Equal(feederIP) || len(transport.sent) != 1 {
		t.Fatalf("result=%v mode=%s sends=%d err=%v", result, mode, len(transport.sent), err)
	}
}

func TestDirectConnectorCompletesVerifiedLoginAndKeepsSessionOpen(t *testing.T) {
	transport := &fakeTransport{}
	transport.onSend = func(packet []byte, address *net.UDPAddr) {
		decoded := tutk.ReverseTransCodePartial(nil, packet)
		if len(decoded) == lanSearchLength && binary.LittleEndian.Uint16(decoded[8:]) == lanSearchOpcode && decoded[lanSearchPhase] == 1 {
			transport.addResponse(tutk.TransCodePartial(nil, testLANSearchResponse(protocolTestUID)), &net.UDPAddr{IP: net.ParseIP("192.0.2.25"), Port: 41135})
			return
		}
		if len(decoded) == loginRequestLength && binary.LittleEndian.Uint16(decoded[8:]) == clientSessionOpcode && binary.LittleEndian.Uint16(decoded[6:]) == 1 {
			var sessionID [8]byte
			copy(sessionID[:], decoded[20:28])
			transport.addResponse(tutk.TransCodePartial(nil, testLoginSuccess(sessionID)), address)
			return
		}
		if len(decoded) < sessionHeaderLength+controlInnerLength || binary.LittleEndian.Uint16(decoded[8:]) != clientSessionOpcode || decoded[sessionHeaderLength] != 0x0C {
			return
		}
		body := decoded[sessionHeaderLength:]
		controlType := binary.LittleEndian.Uint32(body[controlInnerLength:])
		var sessionID [8]byte
		copy(sessionID[:], decoded[20:28])
		switch controlType {
		case controlGetFormat:
			transport.addResponse(tutk.TransCodePartial(nil, testControlReply(sessionID, controlChannelStream, 0, 0x0040)), address)
			transport.addResponse(tutk.TransCodePartial(nil, testControlReply(sessionID, controlChannelStream, 0, controlSetStreamReply)), address)
			transport.addResponse(tutk.TransCodePartial(nil, testControlReply(sessionID, controlChannelSystem, 1, controlGetFormatReply)), address)
		case controlStartVideo:
			if received := transport.ReceiveCount(); received != 5 {
				t.Fatalf("IPCAM_START sent after %d received packets, want discovery, LOGIN, and all three control replies", received)
			}
			transport.addResponse(tutk.TransCodePartial(nil, testVideoFragment(sessionID, 0x4000, 2, 0, false, 0, []byte{0, 0, 0, 1, 0x67, 1})), address)
			transport.addResponse(tutk.TransCodePartial(nil, testVideoFragment(sessionID, 0x4001, 2, 1, true, 0, append([]byte{0, 0, 0, 1, 0x65, 2}, testMediaTrailer(42)...))), address)
		}
	}
	connector := &DirectConnector{
		TransportFactory:  fakeTransportFactory{transport: transport},
		DiscoveryTimeout:  time.Second,
		LoginTimeout:      time.Second,
		DiscoveryTargeter: func() ([]*net.UDPAddr, error) { return []*net.UDPAddr{{IP: net.IPv4bcast, Port: LANPort}}, nil },
		Clock:             func() time.Time { return time.UnixMilli(1_786_544_102_000) },
	}
	states := make([]SessionState, 0, 5)
	session, err := connector.Connect(context.Background(), protocolTestUID, nil, func(event Event) {
		if event.State == StateBootstrapping && event.Step != "" {
			return
		}
		states = append(states, event.State)
	})
	if err != nil {
		t.Fatalf("error=%v", err)
	}
	if session == nil {
		t.Fatal("session is nil")
	}
	if session.Address == nil || !session.Address.IP.Equal(net.ParseIP("192.0.2.25")) || session.Address.Port != 41135 {
		t.Fatalf("session address=%v", session.Address)
	}
	want := []SessionState{StateDiscovering, StateDiscovering, StateDiscovering, StateDiscovering, StateDiscovering, StateDiscovering, StateDiscovering, StateDiscovering, StateLoggingIn, StateLoggingIn, StateLoggingIn, StateConnected, StateBootstrapping, StateStreaming}
	if len(states) != len(want) {
		t.Fatalf("states=%v want=%v", states, want)
	}
	for index := range want {
		if states[index] != want[index] {
			t.Fatalf("states=%v want=%v", states, want)
		}
	}
	if len(transport.sent) != 11 {
		t.Fatalf("wire messages=%d want discovery phases, login, and bootstrap", len(transport.sent))
	}
	phaseOne := tutk.ReverseTransCodePartial(nil, transport.sent[0])
	phaseTwo := tutk.ReverseTransCodePartial(nil, transport.sent[1])
	if binary.LittleEndian.Uint16(phaseOne[8:10]) != lanSearchOpcode || phaseOne[lanSearchPhase] != 1 ||
		binary.LittleEndian.Uint16(phaseTwo[8:10]) != lanSearchOpcode || phaseTwo[lanSearchPhase] != 2 {
		t.Fatalf("unexpected direct-discovery sequence: phase_one=%x phase_two=%x", phaseOne[:12], phaseTwo[:12])
	}
	bootstrapPackets := make([][]byte, 0, 7)
	for _, packet := range transport.sent[4:] {
		bootstrapPackets = append(bootstrapPackets, tutk.ReverseTransCodePartial(nil, packet))
	}
	if len(bootstrapPackets) != 7 || len(bootstrapPackets[0]) != loginRequestLength || len(bootstrapPackets[1]) != loginRequestLength {
		t.Fatalf("unexpected bootstrap login pair: %d packets", len(bootstrapPackets))
	}
	if body := bootstrapPackets[2][sessionHeaderLength:]; len(body) != 16 || body[0] != bootstrapHeartbeatMarker || body[2] != loginCommandVersion {
		t.Fatalf("heartbeat=%x", body)
	}
	assertControlPacket(t, bootstrapPackets[3], controlChannelStream, controlSetStream, streamControlHD[:])
	assertControlPacket(t, bootstrapPackets[4], controlChannelSystem, controlGetFormat, nil)
	if body := bootstrapPackets[5][sessionHeaderLength:]; len(body) != 24 || body[0] != bootstrapAckMarker || binary.LittleEndian.Uint16(body[16:18]) != 1 {
		t.Fatalf("ack=%x", body)
	}
	assertControlPacket(t, bootstrapPackets[6], controlChannelSystem, controlStartVideo, nil)
	startWire := transport.sent[len(transport.sent)-1]
	startDecoded := tutk.ReverseTransCodePartial(nil, startWire)
	if len(startWire) != 68 || len(startDecoded) != 68 || binary.LittleEndian.Uint16(startDecoded[8:10]) != clientSessionOpcode || binary.LittleEndian.Uint16(startDecoded[10:12]) != clientSessionSubtype || binary.LittleEndian.Uint16(startDecoded[6:8]) != 8 {
		t.Fatalf("unexpected IPCAM_START Session16 envelope: wire=%x decoded=%x", startWire, startDecoded)
	}
	if body := startDecoded[sessionHeaderLength:]; len(body) != 40 || binary.LittleEndian.Uint16(body[:2]) != 0x000C || binary.LittleEndian.Uint16(body[16:18]) != controlChannelSystem || binary.LittleEndian.Uint32(body[controlInnerLength:]) != controlStartVideo {
		t.Fatalf("unexpected IPCAM_START body: %x", body)
	}
	if transport.CloseCount() != 0 {
		t.Fatalf("transport closed before session close count=%d", transport.CloseCount())
	}
	if err := session.Close(); err != nil {
		t.Fatal(err)
	}
	if transport.CloseCount() != 1 {
		t.Fatalf("transport close count=%d want=1", transport.CloseCount())
	}
}

func TestDirectConnectorTimesOutDuringBootstrapWithoutMedia(t *testing.T) {
	transport := &fakeTransport{}
	configureDiscoveryResponder(transport, func(packet []byte, address *net.UDPAddr) {
		if len(packet) != loginRequestLength || binary.LittleEndian.Uint16(packet[8:]) != clientSessionOpcode || binary.LittleEndian.Uint16(packet[6:]) != 1 {
			return
		}
		var sessionID [8]byte
		copy(sessionID[:], packet[20:28])
		transport.addResponse(tutk.TransCodePartial(nil, testLoginSuccess(sessionID)), address)
	})
	connector := testDirectConnector(transport, time.Second)
	connector.BootstrapTimeout = 10 * time.Millisecond
	states := make([]SessionState, 0, 6)
	_, err := connector.Connect(context.Background(), protocolTestUID, nil, func(event Event) {
		states = append(states, event.State)
	})
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("error=%v", err)
	}
	if len(states) == 0 || states[len(states)-1] != StateBootstrapping {
		t.Fatalf("states=%v", states)
	}
	if transport.CloseCount() != 1 {
		t.Fatalf("transport close count=%d want=1", transport.CloseCount())
	}
}

func assertControlPacket(t *testing.T, packet []byte, wantChannel uint16, wantType uint32, wantData []byte) {
	t.Helper()
	if len(packet) < sessionHeaderLength+controlInnerLength+4 {
		t.Fatalf("control packet too short: %x", packet)
	}
	body := packet[sessionHeaderLength:]
	if body[0] != 0x0C || body[2] != loginCommandVersion || binary.LittleEndian.Uint16(body[16:18]) != wantChannel || binary.LittleEndian.Uint32(body[controlInnerLength:]) != wantType {
		t.Fatalf("unexpected control body: %x", body)
	}
	if got := body[controlInnerLength+4:]; string(got) != string(wantData) {
		t.Fatalf("control data=%x want=%x", got, wantData)
	}
}

func testControlReply(sessionID [8]byte, channel uint16, subsequence uint16, controlType uint32) []byte {
	body := encodeControlData(0, channel, 0, make([]byte, 4))
	binary.LittleEndian.PutUint16(body[18:20], subsequence)
	binary.LittleEndian.PutUint32(body[controlInnerLength:], controlType)
	return encodeDeviceSessionPacket(sessionID, body)
}

func testVideoFragment(sessionID [8]byte, subsequence uint16, count uint8, index uint16, end bool, frameNumber uint32, payload []byte) []byte {
	body := make([]byte, mediaHeaderLength+len(payload))
	body[0] = 0x0C
	body[2] = loginCommandVersion
	if end {
		body[1] = 0x01
		body[17] = 0x01
	}
	body[16] = h264MainChannel
	binary.LittleEndian.PutUint16(body[18:20], subsequence)
	body[20] = count
	binary.LittleEndian.PutUint16(body[22:24], index)
	binary.LittleEndian.PutUint16(body[24:26], uint16(len(payload)))
	binary.LittleEndian.PutUint32(body[28:32], frameNumber)
	copy(body[mediaHeaderLength:], payload)
	return encodeDeviceSessionPacket(sessionID, body)
}

func testMediaTrailer(timestamp uint32) []byte {
	trailer := make([]byte, mediaMetadataLength)
	trailer[0] = h264CodecMarker
	binary.LittleEndian.PutUint32(trailer[len(trailer)-4:], timestamp)
	return trailer
}

func encodeDeviceSessionPacket(sessionID [8]byte, body []byte) []byte {
	packet := make([]byte, sessionHeaderLength+len(body))
	packet[0] = 0x04
	packet[1] = 0x02
	packet[2] = deviceSessionMagicVersion
	packet[3] = sessionFlags
	binary.LittleEndian.PutUint16(packet[4:6], uint16(len(packet)-16))
	binary.LittleEndian.PutUint16(packet[8:10], deviceSessionOpcode)
	binary.LittleEndian.PutUint16(packet[10:12], deviceLoginSubtype)
	copy(packet[12:28], sessionID16(sessionID))
	copy(packet[sessionHeaderLength:], body)
	return packet
}

func TestDirectConnectorRejectsUnexpectedLoginResponseAndTimesOut(t *testing.T) {
	transport := &fakeTransport{}
	configureDiscoveryResponder(transport, func(packet []byte, address *net.UDPAddr) {
		if len(packet) != loginRequestLength || binary.LittleEndian.Uint16(packet[8:]) != clientSessionOpcode || binary.LittleEndian.Uint16(packet[6:]) != 1 {
			return
		}
		transport.addResponse(tutk.TransCodePartial(nil, testLoginSuccess([8]byte{9, 9, 9, 9, 9, 9, 9, 9})), address)
	})
	connector := testDirectConnector(transport, 10*time.Millisecond)
	_, err := connector.Connect(context.Background(), protocolTestUID, nil, nil)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("error=%v", err)
	}
	if transport.CloseCount() != 1 {
		t.Fatalf("transport close count=%d want=1", transport.CloseCount())
	}
}

func TestDirectConnectorHonorsCancellationDuringLogin(t *testing.T) {
	transport := &fakeTransport{}
	connectionContext, cancel := context.WithCancel(context.Background())
	defer cancel()
	configureDiscoveryResponder(transport, func(packet []byte, _ *net.UDPAddr) {
		if len(packet) == loginRequestLength && binary.LittleEndian.Uint16(packet[8:]) == clientSessionOpcode && binary.LittleEndian.Uint16(packet[6:]) == 1 {
			cancel()
		}
	})
	connector := testDirectConnector(transport, time.Second)
	_, err := connector.Connect(connectionContext, protocolTestUID, nil, nil)
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("error=%v", err)
	}
}

func configureDiscoveryResponder(transport *fakeTransport, onLoginSend func([]byte, *net.UDPAddr)) {
	transport.onSend = func(packet []byte, address *net.UDPAddr) {
		decoded := tutk.ReverseTransCodePartial(nil, packet)
		if len(decoded) == lanSearchLength && binary.LittleEndian.Uint16(decoded[8:]) == lanSearchOpcode && decoded[lanSearchPhase] == 1 {
			transport.addResponse(tutk.TransCodePartial(nil, testLANSearchResponse(protocolTestUID)), &net.UDPAddr{IP: net.ParseIP("192.0.2.25"), Port: 40238})
			return
		}
		onLoginSend(decoded, address)
	}
}

func testDirectConnector(transport DatagramTransport, loginTimeout time.Duration) *DirectConnector {
	return &DirectConnector{
		TransportFactory:  fakeTransportFactory{transport: transport},
		DiscoveryTimeout:  time.Second,
		LoginTimeout:      loginTimeout,
		DiscoveryTargeter: func() ([]*net.UDPAddr, error) { return []*net.UDPAddr{{IP: net.IPv4bcast, Port: LANPort}}, nil },
		Clock:             func() time.Time { return time.UnixMilli(1_786_544_102_000) },
	}
}

func testLANSearchResponse(uid string) []byte {
	packet := make([]byte, lanSearchResponseLength)
	packet[0] = 0x04
	packet[1] = 0x02
	packet[2] = deviceMagicVersion
	packet[3] = controlFlags
	binary.LittleEndian.PutUint16(packet[4:], lanSearchResponseLength-16)
	binary.LittleEndian.PutUint16(packet[8:], lanSearchResponseOpcode)
	binary.LittleEndian.PutUint16(packet[10:], lanSearchResponseSubtype)
	copy(packet[lanSearchResponseUIDOffset:lanSearchResponseUIDOffset+UIDLength], uid)
	return packet
}

type fakeDatagram struct {
	packet  []byte
	address *net.UDPAddr
}

type fakeTransport struct {
	mu         sync.Mutex
	sent       [][]byte
	responses  []fakeDatagram
	onSend     func([]byte, *net.UDPAddr)
	closeCalls int
	receives   int
}

func (transport *fakeTransport) SendTo(packet []byte, address *net.UDPAddr) error {
	copyPacket := append([]byte(nil), packet...)
	transport.mu.Lock()
	transport.sent = append(transport.sent, copyPacket)
	onSend := transport.onSend
	transport.mu.Unlock()
	if onSend != nil {
		onSend(copyPacket, address)
	}
	return nil
}

func (transport *fakeTransport) Receive(ctx context.Context) ([]byte, *net.UDPAddr, error) {
	transport.mu.Lock()
	if len(transport.responses) > 0 {
		response := transport.responses[0]
		transport.responses = transport.responses[1:]
		transport.receives++
		transport.mu.Unlock()
		return response.packet, response.address, nil
	}
	transport.mu.Unlock()
	<-ctx.Done()
	return nil, nil, ctx.Err()
}

func (transport *fakeTransport) ReceiveCount() int {
	transport.mu.Lock()
	defer transport.mu.Unlock()
	return transport.receives
}

func (transport *fakeTransport) LocalAddress() *net.UDPAddr {
	return nil
}

func (transport *fakeTransport) Close() error {
	transport.mu.Lock()
	transport.closeCalls++
	transport.mu.Unlock()
	return nil
}

func (transport *fakeTransport) CloseCount() int {
	transport.mu.Lock()
	defer transport.mu.Unlock()
	return transport.closeCalls
}

func (transport *fakeTransport) addResponse(packet []byte, address *net.UDPAddr) {
	transport.mu.Lock()
	transport.responses = append(transport.responses, fakeDatagram{packet: packet, address: address})
	transport.mu.Unlock()
}

type fakeTransportFactory struct {
	transport DatagramTransport
}

func (factory fakeTransportFactory) Open() (DatagramTransport, error) {
	return factory.transport, nil
}

type fakeRoutedTransportFactory struct {
	transport    DatagramTransport
	destinations []net.IP
}

func (factory *fakeRoutedTransportFactory) Open() (DatagramTransport, error) {
	return factory.transport, nil
}

func (factory *fakeRoutedTransportFactory) OpenForDestination(_ context.Context, destination net.IP) (DatagramTransport, error) {
	factory.destinations = append(factory.destinations, append(net.IP(nil), destination...))
	return factory.transport, nil
}

func TestDirectConnectorResolvesSourcePerFeederDestination(t *testing.T) {
	transport := &fakeTransport{}
	factory := &fakeRoutedTransportFactory{transport: transport}
	connector := DirectConnector{TransportFactory: factory}
	feederA := net.IPv4(10, 3, 100, 90)
	feederB := net.IPv4(10, 3, 100, 91)

	_, targetA, err := connector.openTransport(context.Background(), feederA)
	if err != nil || targetA == nil || !targetA.IP.Equal(feederA) || targetA.Port != LANPort {
		t.Fatalf("target A=%v error=%v", targetA, err)
	}
	_, targetB, err := connector.openTransport(context.Background(), feederB)
	if err != nil || targetB == nil || !targetB.IP.Equal(feederB) || targetB.Port != LANPort {
		t.Fatalf("target B=%v error=%v", targetB, err)
	}
	if len(factory.destinations) != 2 || !factory.destinations[0].Equal(feederA) || !factory.destinations[1].Equal(feederB) {
		t.Fatalf("routed destinations=%v", factory.destinations)
	}
}
