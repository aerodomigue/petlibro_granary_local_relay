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

func TestDecodeKnock2ValidatesIdentityAndWireStructure(t *testing.T) {
	nonce := [8]byte{8, 7, 6, 5, 4, 3, 2, 1}
	packet := testKnock2(protocolTestUID, nonce)
	knock, err := DecodeKnock2(packet, protocolTestUID, nonce)
	if err != nil {
		t.Fatal(err)
	}
	if knock.UID != protocolTestUID || knock.Nonce != nonce {
		t.Fatalf("unexpected knock: %+v", knock)
	}

	t.Run("wrong UID", func(t *testing.T) {
		wrongUID := testKnock2("PLAF2030000000000002", nonce)
		if _, err := DecodeKnock2(wrongUID, protocolTestUID, nonce); !errors.Is(err, ErrUIDMismatch) {
			t.Fatalf("error=%v", err)
		}
	})
	t.Run("wrong magic", func(t *testing.T) {
		wrongMagic := append([]byte(nil), packet...)
		wrongMagic[2] = clientMagicVersion
		if _, err := DecodeKnock2(wrongMagic, protocolTestUID, nonce); !errors.Is(err, ErrUnexpectedPacket) {
			t.Fatalf("error=%v", err)
		}
	})
	t.Run("wrong nonce", func(t *testing.T) {
		wrongNonce := nonce
		wrongNonce[0]++
		if _, err := DecodeKnock2(testKnock2(protocolTestUID, wrongNonce), protocolTestUID, nonce); !errors.Is(err, ErrUnexpectedPacket) {
			t.Fatalf("error=%v", err)
		}
	})
	t.Run("truncated", func(t *testing.T) {
		if _, err := DecodeKnock2(packet[:20], protocolTestUID, nonce); !errors.Is(err, ErrPacketTooShort) {
			t.Fatalf("error=%v", err)
		}
	})
}

func TestEncodeKnockReplyMatchesVerifiedLayout(t *testing.T) {
	nonce := [8]byte{1, 1, 2, 3, 5, 8, 13, 21}
	packet, err := EncodeKnockReply(protocolTestUID, nonce)
	if err != nil {
		t.Fatal(err)
	}
	if len(packet) != knockLength || packet[2] != clientMagicVersion || binary.LittleEndian.Uint16(packet[8:]) != knockReplyOpcode {
		t.Fatalf("unexpected KNOCK_RR2: %x", packet)
	}
	if string(packet[uidOffset:uidOffset+UIDLength]) != protocolTestUID || [8]byte(packet[nonceOffset:nonceOffset+8]) != nonce {
		t.Fatalf("KNOCK_RR2 does not preserve correlation: %x", packet)
	}
}

func TestDiscoverRequiresValidLANSearchResponseThenKnock2(t *testing.T) {
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
		{
			packet:  tutk.TransCodePartial(nil, testKnock2("PLAF2030000000000002", nonce)),
			address: address,
		},
		{
			packet:  tutk.TransCodePartial(nil, testKnock2(protocolTestUID, [8]byte{1, 2, 3, 4, 5, 6, 7, 8})),
			address: address,
		},
		{
			packet:  tutk.TransCodePartial(nil, testKnock2(protocolTestUID, nonce)),
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
		transport.addResponse(tutk.TransCodePartial(nil, testKnock2(capturedResponseUID, [8]byte{})), &net.UDPAddr{IP: knownIP, Port: 41135})
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
		var nonce [8]byte
		copy(nonce[:], decoded[lanSearchNonce:lanSearchNonce+len(nonce)])
		transport.addResponse(tutk.TransCodePartial(nil, testLANSearchResponse(protocolTestUID)), &net.UDPAddr{IP: knownIP, Port: 40238})
		transport.addResponse(tutk.TransCodePartial(nil, testKnock2(protocolTestUID, nonce)), &net.UDPAddr{IP: knownIP, Port: 40238})
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
		var nonce [8]byte
		copy(nonce[:], decoded[lanSearchNonce:lanSearchNonce+len(nonce)])
		transport.addResponse(tutk.TransCodePartial(nil, testLANSearchResponse(protocolTestUID)), &net.UDPAddr{IP: knownIP, Port: 40238})
		transport.addResponse(tutk.TransCodePartial(nil, testKnock2(protocolTestUID, nonce)), &net.UDPAddr{IP: knownIP, Port: 40238})
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
		decoded := tutk.ReverseTransCodePartial(nil, packet)
		var nonce [8]byte
		copy(nonce[:], decoded[lanSearchNonce:lanSearchNonce+len(nonce)])
		transport.addResponse(tutk.TransCodePartial(nil, testLANSearchResponse(protocolTestUID)), &net.UDPAddr{IP: feederIP, Port: 40238})
		transport.addResponse(tutk.TransCodePartial(nil, testKnock2(protocolTestUID, nonce)), &net.UDPAddr{IP: feederIP, Port: 40238})
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
		if len(decoded) == lanSearchLength && binary.LittleEndian.Uint16(decoded[8:]) == lanSearchOpcode {
			var nonce [8]byte
			copy(nonce[:], decoded[lanSearchNonce:lanSearchNonce+len(nonce)])
			transport.addResponse(tutk.TransCodePartial(nil, testLANSearchResponse(protocolTestUID)), &net.UDPAddr{IP: net.ParseIP("192.0.2.25"), Port: 40238})
			transport.addResponse(tutk.TransCodePartial(nil, testKnock2(protocolTestUID, nonce)), &net.UDPAddr{IP: net.ParseIP("192.0.2.25"), Port: 41135})
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
			transport.addResponse(tutk.TransCodePartial(nil, testControlReply(sessionID, controlChannelStream, 0)), address)
			transport.addResponse(tutk.TransCodePartial(nil, testControlReply(sessionID, controlChannelSystem, 1)), address)
		case controlStartVideo:
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
	want := []SessionState{StateDiscovering, StateDiscovering, StateDiscovering, StateKnocking, StateKnocking, StateLoggingIn, StateLoggingIn, StateLoggingIn, StateConnected, StateBootstrapping, StateStreaming}
	if len(states) != len(want) {
		t.Fatalf("states=%v want=%v", states, want)
	}
	for index := range want {
		if states[index] != want[index] {
			t.Fatalf("states=%v want=%v", states, want)
		}
	}
	if len(transport.sent) != 11 {
		t.Fatalf("wire messages=%d want discovery, knock, login, and bootstrap", len(transport.sent))
	}
	search := tutk.ReverseTransCodePartial(nil, transport.sent[0])
	knockReply := tutk.ReverseTransCodePartial(nil, transport.sent[1])
	if binary.LittleEndian.Uint16(search[8:10]) != lanSearchOpcode || search[lanSearchPhase] != 1 ||
		binary.LittleEndian.Uint16(knockReply[8:10]) != knockReplyOpcode {
		t.Fatalf("unexpected preamble sequence: search=%x knock_reply=%x", search[:12], knockReply[:12])
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

func testControlReply(sessionID [8]byte, channel uint16, subsequence uint16) []byte {
	body := encodeControlData(0, channel, 0, make([]byte, 8))
	binary.LittleEndian.PutUint16(body[18:20], subsequence)
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
		if len(decoded) == lanSearchLength && binary.LittleEndian.Uint16(decoded[8:]) == lanSearchOpcode {
			var nonce [8]byte
			copy(nonce[:], decoded[lanSearchNonce:lanSearchNonce+len(nonce)])
			transport.addResponse(tutk.TransCodePartial(nil, testLANSearchResponse(protocolTestUID)), &net.UDPAddr{IP: net.ParseIP("192.0.2.25"), Port: 40238})
			transport.addResponse(tutk.TransCodePartial(nil, testKnock2(protocolTestUID, nonce)), &net.UDPAddr{IP: net.ParseIP("192.0.2.25"), Port: 41135})
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

func testKnock2(uid string, nonce [8]byte) []byte {
	packet := make([]byte, knockLength)
	packet[0] = 0x04
	packet[1] = 0x02
	packet[2] = deviceMagicVersion
	packet[3] = controlFlags
	binary.LittleEndian.PutUint16(packet[4:], knockLength-16)
	binary.LittleEndian.PutUint16(packet[8:], knockOpcode)
	binary.LittleEndian.PutUint16(packet[10:], knockSubtype)
	copy(packet[uidOffset:uidOffset+UIDLength], uid)
	copy(packet[nonceOffset:nonceOffset+len(nonce)], nonce[:])
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
		transport.mu.Unlock()
		return response.packet, response.address, nil
	}
	transport.mu.Unlock()
	<-ctx.Done()
	return nil, nil, ctx.Err()
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
