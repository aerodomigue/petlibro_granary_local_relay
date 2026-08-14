package plaf203

import (
	"context"
	"crypto/rand"
	"errors"
	"fmt"
	"net"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/tutk"
)

const (
	defaultDiscoveryTimeout = 2 * time.Second
	defaultLoginTimeout     = 3 * time.Second
)

// SessionState is the explicit lifecycle of one requested camera attempt.
type SessionState string

const (
	StateIdle        SessionState = "idle"
	StateDiscovering SessionState = "discovering"
	StateKnocking    SessionState = "knocking"
	StateLoggingIn   SessionState = "logging_in"
	StateConnected   SessionState = "connected"
	StateFailed      SessionState = "failed"
)

// Event reports an immutable protocol transition to the owning bridge.
type Event struct {
	State   SessionState
	Address *net.UDPAddr
	Step    string
}

// Observer receives state transitions without receiving the UID or any secret.
type Observer func(Event)

// Connector is used by the bridge and replaced by fakes in unit tests.
type Connector interface {
	Connect(context.Context, string, Observer) (*Session, error)
}

// TransportFactory creates a transport per explicit connection attempt.
type TransportFactory interface {
	Open() (DatagramTransport, error)
}

// DirectConnector implements only the protocol section confirmed against the
// current PLAF203 capture: LAN_SEARCH3, KNOCK2/KNOCK_RR2, and the Session16
// client-start LOGIN pair.
type DirectConnector struct {
	TransportFactory  TransportFactory
	DiscoveryTimeout  time.Duration
	LoginTimeout      time.Duration
	DiscoveryTargeter func() ([]*net.UDPAddr, error)
	Clock             func() time.Time
}

// NewDirectConnector returns a connector with bounded default discovery.
func NewDirectConnector() *DirectConnector {
	return &DirectConnector{
		TransportFactory:  UDPTransportFactory{},
		DiscoveryTimeout:  defaultDiscoveryTimeout,
		LoginTimeout:      defaultLoginTimeout,
		DiscoveryTargeter: DiscoveryTargets,
		Clock:             time.Now,
	}
}

// Connect performs discovery, knock, and the verified V3.0.30 Session16
// client-start pair. It returns an open session only after the feeder's 0x0408
// ACK. The caller owns closing that session.
func (connector *DirectConnector) Connect(ctx context.Context, uid string, observe Observer) (*Session, error) {
	if err := ValidateUID(uid); err != nil {
		return nil, err
	}
	if connector.TransportFactory == nil {
		return nil, errors.New("PLAF203 transport factory is unavailable")
	}
	transport, err := connector.TransportFactory.Open()
	if err != nil {
		return nil, err
	}
	keepTransport := false
	defer func() {
		if !keepTransport {
			_ = transport.Close()
		}
	}()

	var nonce [8]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return nil, fmt.Errorf("generate PLAF203 discovery nonce: %w", err)
	}
	targeter := connector.DiscoveryTargeter
	if targeter == nil {
		targeter = DiscoveryTargets
	}
	targets, err := targeter()
	if err != nil {
		return nil, err
	}
	timeout := connector.DiscoveryTimeout
	if timeout <= 0 {
		timeout = defaultDiscoveryTimeout
	}
	discoveryContext, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	emit(observe, Event{State: StateDiscovering})
	address, err := Discover(discoveryContext, transport, uid, nonce, targets)
	if err != nil {
		return nil, err
	}

	emit(observe, Event{State: StateKnocking, Address: address})
	knockReply, err := EncodeKnockReply(uid, nonce)
	if err != nil {
		return nil, err
	}
	if err := transport.SendTo(tutk.TransCodePartial(nil, knockReply), address); err != nil {
		return nil, fmt.Errorf("send PLAF203 KNOCK_RR2: %w", err)
	}

	emit(observe, Event{State: StateLoggingIn, Address: address})
	clock := connector.Clock
	if clock == nil {
		clock = time.Now
	}
	timestampMillis := uint32(clock().UnixMilli())
	loginRequests := [2]LoginRequest{
		{SessionID: nonce, Sequence: 0, Variant: LoginPrimary, TimestampMillis: timestampMillis},
		{SessionID: nonce, Sequence: 1, Variant: LoginSecondary, TimestampMillis: timestampMillis + 1},
	}
	for _, loginRequest := range loginRequests {
		encoded, encodeErr := loginRequest.Encode()
		if encodeErr != nil {
			return nil, encodeErr
		}
		step := "client_start_primary"
		if loginRequest.Variant == LoginSecondary {
			step = "client_start_secondary"
		}
		emit(observe, Event{State: StateLoggingIn, Address: address, Step: step})
		if sendErr := transport.SendTo(tutk.TransCodePartial(nil, encoded), address); sendErr != nil {
			return nil, fmt.Errorf("send PLAF203 login: %w", sendErr)
		}
	}
	loginTimeout := connector.LoginTimeout
	if loginTimeout <= 0 {
		loginTimeout = defaultLoginTimeout
	}
	loginContext, loginCancel := context.WithTimeout(ctx, loginTimeout)
	defer loginCancel()
	for {
		response, _, receiveErr := transport.Receive(loginContext)
		if receiveErr != nil {
			return nil, fmt.Errorf("receive PLAF203 login response: %w", receiveErr)
		}
		decoded := tutk.ReverseTransCodePartial(nil, response)
		if _, decodeErr := DecodeLoginResponse(decoded, nonce); decodeErr != nil {
			continue
		}
		keepTransport = true
		return &Session{ID: nonce, Address: cloneUDPAddress(address), transport: transport}, nil
	}
}

func cloneUDPAddress(address *net.UDPAddr) *net.UDPAddr {
	if address == nil {
		return nil
	}
	return &net.UDPAddr{IP: append(net.IP(nil), address.IP...), Port: address.Port, Zone: address.Zone}
}

func emit(observer Observer, event Event) {
	if observer != nil {
		observer(event)
	}
}
