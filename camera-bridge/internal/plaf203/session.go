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
	mediaStatsLogInterval   = 5 * time.Second
)

// SessionState is the explicit lifecycle of one requested camera attempt.
type SessionState string

const (
	StateIdle          SessionState = "idle"
	StateDiscovering   SessionState = "discovering"
	StateKnocking      SessionState = "knocking"
	StateLoggingIn     SessionState = "logging_in"
	StateConnected     SessionState = "connected"
	StateBootstrapping SessionState = "bootstrapping"
	StateStreaming     SessionState = "streaming"
	StateFailed        SessionState = "failed"
)

// Event reports an immutable protocol transition to the owning bridge.
type Event struct {
	State   SessionState
	Address *net.UDPAddr
	Step    string
	Frame   *VideoFrame
}

// Observer receives state transitions without receiving the UID or any secret.
type Observer func(Event)

// Connector is used by the bridge and replaced by fakes in unit tests.
type Connector interface {
	Connect(context.Context, string, net.IP, Observer) (*Session, error)
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
	BootstrapTimeout  time.Duration
	BroadcastFallback bool
	DiscoveryTargeter func() ([]*net.UDPAddr, error)
	Clock             func() time.Time
}

// NewDirectConnector returns a connector with bounded default discovery.
func NewDirectConnector() *DirectConnector {
	return &DirectConnector{
		TransportFactory:  UDPTransportFactory{},
		DiscoveryTimeout:  defaultDiscoveryTimeout,
		LoginTimeout:      defaultLoginTimeout,
		BootstrapTimeout:  defaultBootstrapTimeout,
		BroadcastFallback: true,
		DiscoveryTargeter: DiscoveryTargets,
		Clock:             time.Now,
	}
}

// Connect performs discovery, knock, and the verified V3.0.30 Session16
// client-start pair. It returns an open session only after the feeder's 0x0408
// ACK. The caller owns closing that session.
func (connector *DirectConnector) Connect(ctx context.Context, uid string, feederIP net.IP, observe Observer) (*Session, error) {
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
	timeout := connector.DiscoveryTimeout
	if timeout <= 0 {
		timeout = defaultDiscoveryTimeout
	}
	address, discoveryMode, err := connector.discover(ctx, transport, uid, nonce, feederIP, timeout, observe)
	if err != nil {
		return nil, err
	}

	emit(observe, Event{State: StateKnocking, Address: address, Step: discoveryMode})
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
		session := &Session{
			ID:             nonce,
			Address:        cloneUDPAddress(address),
			transport:      transport,
			clock:          clock,
			sequence:       2,
			controlCounter: 0,
			media:          NewMediaReceiver(),
			observer:       observe,
		}
		emit(observe, Event{State: StateConnected, Address: address})
		emit(observe, Event{State: StateBootstrapping, Address: address})
		bootstrapTimeout := connector.BootstrapTimeout
		if bootstrapTimeout <= 0 {
			bootstrapTimeout = defaultBootstrapTimeout
		}
		bootstrapContext, bootstrapCancel := context.WithTimeout(ctx, bootstrapTimeout)
		frame, bootstrapErr := session.bootstrap(bootstrapContext)
		bootstrapCancel()
		if bootstrapErr != nil {
			return nil, fmt.Errorf("bootstrap PLAF203 media: %w", bootstrapErr)
		}
		session.startMediaReceiver()
		session.noteMediaEvent(clock())
		keepTransport = true
		emit(observe, Event{State: StateStreaming, Address: address, Step: "bootstrap", Frame: frame})
		return session, nil
	}
}

func (connector *DirectConnector) discover(ctx context.Context, transport DatagramTransport, uid string, nonce [8]byte, feederIP net.IP, timeout time.Duration, observe Observer) (*net.UDPAddr, string, error) {
	if ipv4 := feederIP.To4(); ipv4 != nil && !ipv4.IsUnspecified() {
		target := (&net.UDPAddr{IP: ipv4, Port: LANPort}).String()
		emit(observe, Event{State: StateDiscovering, Step: "unicast target=" + target})
		unicastContext, cancel := context.WithTimeout(ctx, timeout)
		address, err := DiscoverUnicast(unicastContext, transport, ipv4, uid, nonce)
		cancel()
		if err == nil {
			return address, "unicast", nil
		}
		emit(observe, Event{State: StateDiscovering, Step: "unicast_timeout"})
		if !connector.BroadcastFallback {
			return nil, "unicast", err
		}
		emit(observe, Event{State: StateDiscovering, Step: "broadcast_fallback"})
	}
	targeter := connector.DiscoveryTargeter
	if targeter == nil {
		targeter = DiscoveryTargets
	}
	targets, err := targeter()
	if err != nil {
		return nil, "broadcast", err
	}
	emit(observe, Event{State: StateDiscovering, Step: "broadcast"})
	broadcastContext, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	address, err := Discover(broadcastContext, transport, uid, nonce, targets)
	return address, "broadcast", err
}

func (session *Session) startMediaReceiver() {
	receiveContext, cancel := context.WithCancel(context.Background())
	session.cancelReceive = cancel
	go func() {
		for {
			packet, _, err := session.transport.Receive(receiveContext)
			if err != nil {
				return
			}
			frame, parseErr := session.media.HandlePacket(tutk.ReverseTransCodePartial(nil, packet), session.ID, session.clock())
			if parseErr != nil || frame == nil || !session.noteMediaEvent(session.clock()) {
				continue
			}
			emit(session.observer, Event{State: StateStreaming, Address: session.Address, Step: "media_stats", Frame: frame})
		}
	}()
}

func (session *Session) noteMediaEvent(now time.Time) bool {
	session.mediaMu.Lock()
	defer session.mediaMu.Unlock()
	if now.Sub(session.lastMediaEvent) < mediaStatsLogInterval {
		return false
	}
	session.lastMediaEvent = now
	return true
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
