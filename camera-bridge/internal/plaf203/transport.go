package plaf203

import (
	"context"
	"fmt"
	"net"
	"time"
)

const readPollInterval = 100 * time.Millisecond

// SourceResolver determines the kernel-selected IPv4 source for one feeder.
// It does not send an application datagram.
type SourceResolver interface {
	ResolveSourceIP(context.Context, net.IP) (net.IP, error)
}

// KernelSourceResolver asks the kernel to select a route and source address.
type KernelSourceResolver struct{}

// RouteSourceError identifies a failed route probe without losing its target.
type RouteSourceError struct {
	Destination net.IP
	Err         error
}

// Error returns a safe route-probe failure description.
func (err *RouteSourceError) Error() string {
	return fmt.Sprintf("resolve PLAF203 UDP source for %s: %v", err.Destination, err.Err)
}

// Unwrap returns the underlying network error.
func (err *RouteSourceError) Unwrap() error {
	return err.Err
}

// ResolveSourceIP returns the IPv4 source the kernel would use for destination.
func (KernelSourceResolver) ResolveSourceIP(ctx context.Context, destination net.IP) (net.IP, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	ipv4 := destination.To4()
	if ipv4 == nil || ipv4.IsUnspecified() {
		return nil, fmt.Errorf("destination must be a valid IPv4 address")
	}
	probe, err := net.DialUDP("udp4", nil, &net.UDPAddr{IP: ipv4, Port: LANPort})
	if err != nil {
		return nil, err
	}
	defer probe.Close()
	local, ok := probe.LocalAddr().(*net.UDPAddr)
	if !ok || local.IP == nil {
		return nil, fmt.Errorf("kernel did not select a UDP source address")
	}
	source := local.IP.To4()
	if source == nil || source.IsUnspecified() {
		return nil, fmt.Errorf("kernel selected an invalid IPv4 source address")
	}
	return append(net.IP(nil), source...), nil
}

// UDPTransportFactory creates real UDP transports only when an explicit
// connection attempt is requested.
type UDPTransportFactory struct {
	SourceResolver SourceResolver
}

// Open returns an IPv4 UDP transport bound to an ephemeral local port.
func (UDPTransportFactory) Open() (DatagramTransport, error) {
	return openUDPTransport(nil)
}

// OpenForDestination creates a non-connected UDP socket bound to the source
// address the kernel selected for the destination feeder.
func (factory UDPTransportFactory) OpenForDestination(ctx context.Context, destination net.IP) (DatagramTransport, error) {
	resolver := factory.SourceResolver
	if resolver == nil {
		resolver = KernelSourceResolver{}
	}
	source, err := resolver.ResolveSourceIP(ctx, destination)
	if err != nil {
		return nil, &RouteSourceError{Destination: append(net.IP(nil), destination...), Err: err}
	}
	return openUDPTransport(&net.UDPAddr{IP: source, Port: 0})
}

func openUDPTransport(localAddress *net.UDPAddr) (DatagramTransport, error) {
	connection, err := net.ListenUDP("udp4", localAddress)
	if err != nil {
		return nil, fmt.Errorf("bind PLAF203 UDP transport: %w", err)
	}
	return &udpTransport{connection: connection}, nil
}

type udpSocket interface {
	WriteToUDP([]byte, *net.UDPAddr) (int, error)
	ReadFromUDP([]byte) (int, *net.UDPAddr, error)
	SetReadDeadline(time.Time) error
	LocalAddr() net.Addr
	Close() error
}

type udpTransport struct {
	connection udpSocket
}

func (transport *udpTransport) SendTo(packet []byte, address *net.UDPAddr) error {
	written, err := transport.connection.WriteToUDP(packet, address)
	if err != nil {
		return err
	}
	if written != len(packet) {
		return fmt.Errorf("PLAF203 UDP short write: got=%d want=%d", written, len(packet))
	}
	return nil
}

func (transport *udpTransport) Receive(ctx context.Context) ([]byte, *net.UDPAddr, error) {
	buffer := make([]byte, 2048)
	for {
		if err := ctx.Err(); err != nil {
			return nil, nil, err
		}
		deadline := time.Now().Add(readPollInterval)
		if contextDeadline, hasDeadline := ctx.Deadline(); hasDeadline && contextDeadline.Before(deadline) {
			deadline = contextDeadline
		}
		if err := transport.connection.SetReadDeadline(deadline); err != nil {
			return nil, nil, fmt.Errorf("set PLAF203 read deadline: %w", err)
		}
		length, address, err := transport.connection.ReadFromUDP(buffer)
		if err == nil {
			return append([]byte(nil), buffer[:length]...), address, nil
		}
		if networkError, isNetworkError := err.(net.Error); isNetworkError && networkError.Timeout() {
			continue
		}
		return nil, nil, err
	}
}

func (transport *udpTransport) Close() error {
	return transport.connection.Close()
}

func (transport *udpTransport) LocalAddress() *net.UDPAddr {
	localAddress, ok := transport.connection.LocalAddr().(*net.UDPAddr)
	if !ok || localAddress == nil {
		return nil
	}
	return cloneUDPAddress(localAddress)
}
