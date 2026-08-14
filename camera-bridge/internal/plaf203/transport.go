package plaf203

import (
	"context"
	"fmt"
	"net"
	"time"
)

const readPollInterval = 100 * time.Millisecond

// UDPTransportFactory creates real UDP transports only when an explicit
// connection attempt is requested.
type UDPTransportFactory struct{}

// Open returns an IPv4 UDP transport bound to an ephemeral local port.
func (UDPTransportFactory) Open() (DatagramTransport, error) {
	connection, err := net.ListenUDP("udp4", nil)
	if err != nil {
		return nil, fmt.Errorf("bind PLAF203 UDP transport: %w", err)
	}
	return &udpTransport{connection: connection}, nil
}

type udpTransport struct {
	connection *net.UDPConn
}

func (transport *udpTransport) SendTo(packet []byte, address *net.UDPAddr) error {
	_, err := transport.connection.WriteToUDP(packet, address)
	return err
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
