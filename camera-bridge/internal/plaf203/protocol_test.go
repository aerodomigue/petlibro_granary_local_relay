package plaf203

import (
	"context"
	"encoding/binary"
	"errors"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/tutk"
)

const protocolTestUID = "PLAF2030000000000001"

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

func TestDiscoverAcceptsOnlyMatchingUIDAndNonce(t *testing.T) {
	nonce := [8]byte{9, 8, 7, 6, 5, 4, 3, 2}
	address := &net.UDPAddr{IP: net.ParseIP("192.0.2.20"), Port: 40238}
	transport := &fakeTransport{responses: []fakeDatagram{
		{
			packet:  tutk.TransCodePartial(nil, testKnock2("PLAF2030000000000002", nonce)),
			address: &net.UDPAddr{IP: net.ParseIP("192.0.2.21"), Port: 40238},
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

func TestDirectConnectorCompletesVerifiedLoginAndKeepsSessionOpen(t *testing.T) {
	transport := &fakeTransport{}
	transport.onSend = func(packet []byte, address *net.UDPAddr) {
		decoded := tutk.ReverseTransCodePartial(nil, packet)
		if len(decoded) == lanSearchLength && binary.LittleEndian.Uint16(decoded[8:]) == lanSearchOpcode {
			var nonce [8]byte
			copy(nonce[:], decoded[lanSearchNonce:lanSearchNonce+len(nonce)])
			transport.addResponse(tutk.TransCodePartial(nil, testKnock2(protocolTestUID, nonce)), &net.UDPAddr{IP: net.ParseIP("192.0.2.25"), Port: 40238})
			return
		}
		if len(decoded) == loginRequestLength && binary.LittleEndian.Uint16(decoded[8:]) == clientSessionOpcode && binary.LittleEndian.Uint16(decoded[6:]) == 1 {
			var sessionID [8]byte
			copy(sessionID[:], decoded[20:28])
			transport.addResponse(tutk.TransCodePartial(nil, testLoginSuccess(sessionID)), address)
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
	session, err := connector.Connect(context.Background(), protocolTestUID, func(event Event) {
		states = append(states, event.State)
	})
	if err != nil {
		t.Fatalf("error=%v", err)
	}
	if session == nil {
		t.Fatal("session is nil")
	}
	if session.Address == nil || !session.Address.IP.Equal(net.ParseIP("192.0.2.25")) || session.Address.Port != 40238 {
		t.Fatalf("session address=%v", session.Address)
	}
	want := []SessionState{StateDiscovering, StateKnocking, StateLoggingIn, StateLoggingIn, StateLoggingIn}
	if len(states) != len(want) {
		t.Fatalf("states=%v want=%v", states, want)
	}
	for index := range want {
		if states[index] != want[index] {
			t.Fatalf("states=%v want=%v", states, want)
		}
	}
	if len(transport.sent) != 4 {
		t.Fatalf("wire messages=%d want discovery, KNOCK_RR2, and login pair", len(transport.sent))
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

func TestDirectConnectorRejectsUnexpectedLoginResponseAndTimesOut(t *testing.T) {
	transport := &fakeTransport{}
	configureDiscoveryResponder(transport, func(packet []byte, address *net.UDPAddr) {
		if len(packet) != loginRequestLength || binary.LittleEndian.Uint16(packet[8:]) != clientSessionOpcode || binary.LittleEndian.Uint16(packet[6:]) != 1 {
			return
		}
		transport.addResponse(tutk.TransCodePartial(nil, testLoginSuccess([8]byte{9, 9, 9, 9, 9, 9, 9, 9})), address)
	})
	connector := testDirectConnector(transport, 10*time.Millisecond)
	_, err := connector.Connect(context.Background(), protocolTestUID, nil)
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
	_, err := connector.Connect(connectionContext, protocolTestUID, nil)
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
			transport.addResponse(tutk.TransCodePartial(nil, testKnock2(protocolTestUID, nonce)), &net.UDPAddr{IP: net.ParseIP("192.0.2.25"), Port: 40238})
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
