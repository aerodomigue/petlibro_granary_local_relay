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

const defaultDiscoveryTimeout = 2 * time.Second

var (
	// ErrLoginUnsupported prevents a false Connected state. The supplied fork's
	// LOGIN A/B conflicts with the PLAF203 V3.0.30 PCAP, which contains a
	// different 598-byte post-knock exchange.
	ErrLoginUnsupported = errors.New("PLAF203 LOGIN is not enabled: the V3.0.30 login exchange is not yet confirmed")
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
}

// Observer receives state transitions without receiving the UID or any secret.
type Observer func(Event)

// Connector is used by the bridge and replaced by fakes in unit tests.
type Connector interface {
	Connect(context.Context, string, Observer) error
}

// TransportFactory creates a transport per explicit connection attempt.
type TransportFactory interface {
	Open() (DatagramTransport, error)
}

// DirectConnector implements only the protocol section confirmed against the
// current PLAF203 capture: LAN_SEARCH3 followed by KNOCK2/KNOCK_RR2.
type DirectConnector struct {
	TransportFactory  TransportFactory
	DiscoveryTimeout  time.Duration
	DiscoveryTargeter func() ([]*net.UDPAddr, error)
}

// NewDirectConnector returns a connector with bounded default discovery.
func NewDirectConnector() *DirectConnector {
	return &DirectConnector{
		TransportFactory:  UDPTransportFactory{},
		DiscoveryTimeout:  defaultDiscoveryTimeout,
		DiscoveryTargeter: DiscoveryTargets,
	}
}

// Connect performs discovery and knock. LOGIN is deliberately not sent until
// its V3.0.30 packet structure is independently confirmed.
func (connector *DirectConnector) Connect(ctx context.Context, uid string, observe Observer) error {
	if err := ValidateUID(uid); err != nil {
		return err
	}
	if connector.TransportFactory == nil {
		return errors.New("PLAF203 transport factory is unavailable")
	}
	transport, err := connector.TransportFactory.Open()
	if err != nil {
		return err
	}
	defer func() {
		_ = transport.Close()
	}()

	var nonce [8]byte
	if _, err := rand.Read(nonce[:]); err != nil {
		return fmt.Errorf("generate PLAF203 discovery nonce: %w", err)
	}
	targeter := connector.DiscoveryTargeter
	if targeter == nil {
		targeter = DiscoveryTargets
	}
	targets, err := targeter()
	if err != nil {
		return err
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
		return err
	}

	emit(observe, Event{State: StateKnocking, Address: address})
	knockReply, err := EncodeKnockReply(uid, nonce)
	if err != nil {
		return err
	}
	if err := transport.SendTo(tutk.TransCodePartial(nil, knockReply), address); err != nil {
		return fmt.Errorf("send PLAF203 KNOCK_RR2: %w", err)
	}

	emit(observe, Event{State: StateLoggingIn, Address: address})
	return ErrLoginUnsupported
}

func emit(observer Observer, event Event) {
	if observer != nil {
		observer(event)
	}
}
