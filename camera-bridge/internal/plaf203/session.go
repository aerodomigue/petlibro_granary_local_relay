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
	State                SessionState
	Address              *net.UDPAddr
	LocalAddress         *net.UDPAddr
	Step                 string
	PacketLength         int
	BodyLength           int
	Opcode               uint16
	Sequence             uint16
	SessionChannel       uint16
	SessionCommand       uint16
	ControlType          uint32
	Session25SeqSendCmd1 uint16
	Session25SeqSendCmd2 uint16
	Session25SeqRecvCmd2 uint16
	Session25SeqRecvPkt0 uint16
	Session25SeqRecvPkt1 uint16
	Session25SeqSendCnt  uint16
	Reason               string
	PacketCount          uint64
	ByteCount            uint64
	Types                string
	Rejected             string
	Error                string
	DecodedHex           string
	WireHex              string
	Frame                *VideoFrame
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

// DirectConnector implements the direct-LAN protocol section confirmed for
// PLAF203: LAN_SEARCH3 phases 1 and 2, followed by the existing Session16
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

// Connect performs direct LAN discovery and the verified V3.0.30 Session16
// client-start pair. It returns an open session only after the feeder's 0x0408
// ACK. The caller owns closing that session.
func (connector *DirectConnector) Connect(ctx context.Context, uid string, feederIP net.IP, observe Observer) (*Session, error) {
	if err := ValidateUID(uid); err != nil {
		return nil, err
	}
	if connector.TransportFactory == nil {
		return nil, errors.New("PLAF203 transport factory is unavailable")
	}
	transport, routeTarget, err := connector.openTransport(ctx, feederIP)
	if err != nil {
		var routeErr *RouteSourceError
		if errors.As(err, &routeErr) {
			emit(observe, Event{
				State:   StateDiscovering,
				Step:    "route_failed",
				Address: &net.UDPAddr{IP: append(net.IP(nil), routeErr.Destination...), Port: LANPort},
				Error:   routeErr.Err.Error(),
			})
		}
		return nil, err
	}
	keepTransport := false
	defer func() {
		if !keepTransport {
			_ = transport.Close()
		}
	}()
	localAddress := transport.LocalAddress()
	if routeTarget != nil {
		emit(observe, Event{
			State:        StateDiscovering,
			Step:         "route_source",
			Address:      cloneUDPAddress(routeTarget),
			LocalAddress: localAddress,
		})
	}
	emit(observe, Event{State: StateDiscovering, Step: "udp_socket", LocalAddress: localAddress})

	var nonce [8]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return nil, fmt.Errorf("generate PLAF203 discovery nonce: %w", err)
	}
	timeout := connector.DiscoveryTimeout
	if timeout <= 0 {
		timeout = defaultDiscoveryTimeout
	}
	address, _, err := connector.discover(ctx, transport, uid, nonce, feederIP, timeout, observe)
	if err != nil {
		return nil, err
	}

	if err := CompleteDirectDiscovery(transport, address, uid, nonce, observe); err != nil {
		return nil, err
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
			ID:        nonce,
			Address:   cloneUDPAddress(address),
			transport: transport,
			clock:     clock,
			sequence:  2,
			session25: newSession25State(),
			media:     NewMediaReceiver(),
			observer:  observe,
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
		session.publishFrame(frame)
		session.noteMediaEvent(clock())
		keepTransport = true
		emit(observe, Event{State: StateStreaming, Address: address, Step: "bootstrap", Frame: frame})
		return session, nil
	}
}

func (connector *DirectConnector) openTransport(ctx context.Context, feederIP net.IP) (DatagramTransport, *net.UDPAddr, error) {
	if ipv4 := feederIP.To4(); ipv4 != nil && !ipv4.IsUnspecified() {
		if factory, supportsRoutedSource := connector.TransportFactory.(routedTransportFactory); supportsRoutedSource {
			transport, err := factory.OpenForDestination(ctx, ipv4)
			return transport, &net.UDPAddr{IP: append(net.IP(nil), ipv4...), Port: LANPort}, err
		}
	}
	transport, err := connector.TransportFactory.Open()
	return transport, nil, err
}

func (connector *DirectConnector) discover(ctx context.Context, transport DatagramTransport, uid string, nonce [8]byte, feederIP net.IP, timeout time.Duration, observe Observer) (*net.UDPAddr, string, error) {
	if ipv4 := feederIP.To4(); ipv4 != nil && !ipv4.IsUnspecified() {
		target := (&net.UDPAddr{IP: ipv4, Port: LANPort}).String()
		emit(observe, Event{State: StateDiscovering, Step: "unicast target=" + target})
		unicastContext, cancel := context.WithTimeout(ctx, timeout)
		address, err := discover(unicastContext, transport, uid, nonce, []*net.UDPAddr{{IP: ipv4, Port: LANPort}}, ipv4, observe)
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
	address, err := discover(broadcastContext, transport, uid, nonce, targets, nil, observe)
	return address, "broadcast", err
}

func (session *Session) startMediaReceiver() {
	receiveContext, cancel := context.WithCancel(context.Background())
	session.cancelReceive = cancel
	diagnostics := newBootstrapDiagnostics(session.ID, session.observer)
	go func() {
		for {
			packet, peer, err := session.transport.Receive(receiveContext)
			if err != nil {
				return
			}
			decoded := tutk.ReverseTransCodePartial(nil, packet)
			if handled, keepaliveErr := session.replyKeepalive(decoded, peer); keepaliveErr != nil {
				return
			} else if handled {
				continue
			}
			if handled, heartbeatErr := session.replySessionHeartbeat(decoded, peer, diagnostics); heartbeatErr != nil {
				return
			} else if handled {
				continue
			}
			if inner, decodeErr := decodeDeviceSession(decoded, session.ID); decodeErr == nil {
				if handled, countersErr := session.replySession25Counters(inner, peer, diagnostics); countersErr != nil {
					return
				} else if handled {
					continue
				}
			}
			frame, parseErr := session.media.HandlePacket(decoded, session.ID, session.clock())
			if parseErr != nil || frame == nil {
				continue
			}
			session.noteSession25Media(decoded)
			session.publishFrame(frame)
			if !session.noteMediaEvent(session.clock()) {
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
