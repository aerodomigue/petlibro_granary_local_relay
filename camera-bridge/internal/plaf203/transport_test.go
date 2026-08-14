package plaf203

import (
	"context"
	"errors"
	"net"
	"testing"
	"time"
)

type staticSourceResolver struct {
	sources map[string]net.IP
	err     error
	called  []net.IP
}

func (resolver *staticSourceResolver) ResolveSourceIP(_ context.Context, destination net.IP) (net.IP, error) {
	resolver.called = append(resolver.called, append(net.IP(nil), destination...))
	if resolver.err != nil {
		return nil, resolver.err
	}
	return append(net.IP(nil), resolver.sources[destination.String()]...), nil
}

type fakeUDPSocket struct {
	local       net.Addr
	writeLength int
	writeErr    error
}

func (socket *fakeUDPSocket) WriteToUDP(packet []byte, _ *net.UDPAddr) (int, error) {
	if socket.writeErr != nil {
		return 0, socket.writeErr
	}
	return socket.writeLength, nil
}

func (socket *fakeUDPSocket) ReadFromUDP(_ []byte) (int, *net.UDPAddr, error) {
	return 0, nil, errors.New("not implemented")
}

func (socket *fakeUDPSocket) SetReadDeadline(_ time.Time) error {
	return nil
}

func (socket *fakeUDPSocket) LocalAddr() net.Addr {
	return socket.local
}

func (socket *fakeUDPSocket) Close() error {
	return nil
}

func TestKernelSourceResolverUsesKernelSelectedLoopbackSource(t *testing.T) {
	source, err := (KernelSourceResolver{}).ResolveSourceIP(context.Background(), net.IPv4(127, 0, 0, 1))
	if err != nil {
		t.Fatal(err)
	}
	if !source.Equal(net.IPv4(127, 0, 0, 1)) {
		t.Fatalf("source=%s want=127.0.0.1", source)
	}
}

func TestUDPTransportFactoryBindsKernelResolvedSourceWithEphemeralPort(t *testing.T) {
	destination := net.IPv4(10, 3, 100, 90)
	resolver := &staticSourceResolver{sources: map[string]net.IP{
		destination.String(): net.IPv4(127, 0, 0, 1),
	}}
	factory := UDPTransportFactory{SourceResolver: resolver}
	transport, err := factory.OpenForDestination(context.Background(), destination)
	if err != nil {
		t.Fatal(err)
	}
	defer transport.Close()

	local := transport.LocalAddress()
	if local == nil || !local.IP.Equal(net.IPv4(127, 0, 0, 1)) || local.Port == 0 {
		t.Fatalf("local=%v want loopback with ephemeral port", local)
	}
	if len(resolver.called) != 1 || !resolver.called[0].Equal(destination) {
		t.Fatalf("resolved destinations=%v want=%v", resolver.called, destination)
	}
}

func TestUDPTransportFactoryReturnsRouteProbeFailure(t *testing.T) {
	destination := net.IPv4(10, 3, 100, 90)
	resolver := &staticSourceResolver{err: errors.New("network is unreachable")}
	factory := UDPTransportFactory{SourceResolver: resolver}

	_, err := factory.OpenForDestination(context.Background(), destination)
	var routeErr *RouteSourceError
	if !errors.As(err, &routeErr) || !routeErr.Destination.Equal(destination) {
		t.Fatalf("error=%v", err)
	}
}

func TestUDPTransportSendToAcceptsCompleteWrite(t *testing.T) {
	payload := []byte{1, 2, 3}
	transport := &udpTransport{connection: &fakeUDPSocket{writeLength: len(payload)}}

	if err := transport.SendTo(payload, &net.UDPAddr{IP: net.IPv4(10, 3, 100, 90), Port: LANPort}); err != nil {
		t.Fatal(err)
	}
}

func TestUDPTransportSendToReturnsWriteFailureImmediately(t *testing.T) {
	writeErr := errors.New("network is unreachable")
	transport := &udpTransport{connection: &fakeUDPSocket{writeErr: writeErr}}

	if err := transport.SendTo([]byte{1, 2, 3}, &net.UDPAddr{IP: net.IPv4(10, 3, 100, 90), Port: LANPort}); !errors.Is(err, writeErr) {
		t.Fatalf("error=%v", err)
	}
}

func TestUDPTransportSendToRejectsShortWrite(t *testing.T) {
	transport := &udpTransport{connection: &fakeUDPSocket{writeLength: 2}}

	if err := transport.SendTo([]byte{1, 2, 3}, &net.UDPAddr{IP: net.IPv4(10, 3, 100, 90), Port: LANPort}); err == nil {
		t.Fatal("short write was accepted")
	}
}
