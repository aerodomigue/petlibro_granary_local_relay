package plaf203

import (
	"context"
	"fmt"
	"net"
	"sort"

	"github.com/AlexxIT/go2rtc/pkg/tutk"
)

// DatagramTransport is deliberately small so protocol tests never open a real
// socket. Its payloads are wire-encrypted UDP datagrams.
type DatagramTransport interface {
	SendTo(packet []byte, address *net.UDPAddr) error
	Receive(context.Context) ([]byte, *net.UDPAddr, error)
	Close() error
}

// Discover sends a UID-specific LAN_SEARCH3 probe and accepts only a KNOCK2
// that proves both the requested UID and the generated nonce.
func Discover(ctx context.Context, transport DatagramTransport, uid string, nonce [8]byte, targets []*net.UDPAddr) (*net.UDPAddr, error) {
	if len(targets) == 0 {
		return nil, fmt.Errorf("PLAF203 discovery has no targets")
	}
	search, err := EncodeLANSearch3(uid, nonce)
	if err != nil {
		return nil, err
	}
	wireSearch := tutk.TransCodePartial(nil, search)
	for _, target := range targets {
		if target == nil || target.IP == nil {
			continue
		}
		if err := transport.SendTo(wireSearch, target); err != nil {
			return nil, fmt.Errorf("PLAF203 discovery send %s: %w", target, err)
		}
	}

	for {
		packet, address, receiveErr := transport.Receive(ctx)
		if receiveErr != nil {
			return nil, fmt.Errorf("PLAF203 discovery receive: %w", receiveErr)
		}
		if address == nil || address.IP == nil {
			continue
		}
		decoded := tutk.ReverseTransCodePartial(nil, packet)
		if _, decodeErr := DecodeKnock2(decoded, uid, nonce); decodeErr != nil {
			continue
		}
		return &net.UDPAddr{IP: append(net.IP(nil), address.IP...), Port: address.Port}, nil
	}
}

// DiscoveryTargets returns the global broadcast and every usable interface
// broadcast address. No unbounded subnet scan is performed.
func DiscoveryTargets() ([]*net.UDPAddr, error) {
	interfaces, err := net.Interfaces()
	if err != nil {
		return nil, fmt.Errorf("enumerate network interfaces: %w", err)
	}
	seen := map[string]struct{}{}
	targets := make([]*net.UDPAddr, 0, len(interfaces)+1)
	add := func(ip net.IP) {
		ipv4 := ip.To4()
		if ipv4 == nil || ipv4.IsUnspecified() {
			return
		}
		key := ipv4.String()
		if _, exists := seen[key]; exists {
			return
		}
		seen[key] = struct{}{}
		targets = append(targets, &net.UDPAddr{IP: append(net.IP(nil), ipv4...), Port: LANPort})
	}
	add(net.IPv4bcast)
	for _, networkInterface := range interfaces {
		if networkInterface.Flags&net.FlagUp == 0 || networkInterface.Flags&net.FlagLoopback != 0 || networkInterface.Flags&net.FlagBroadcast == 0 {
			continue
		}
		addresses, addressErr := networkInterface.Addrs()
		if addressErr != nil {
			continue
		}
		for _, address := range addresses {
			ipNetwork, isNetwork := address.(*net.IPNet)
			if !isNetwork {
				continue
			}
			ipv4 := ipNetwork.IP.To4()
			mask := ipNetwork.Mask
			if ipv4 == nil || len(mask) != net.IPv4len {
				continue
			}
			add(net.IPv4(ipv4[0]|^mask[0], ipv4[1]|^mask[1], ipv4[2]|^mask[2], ipv4[3]|^mask[3]))
		}
	}
	sort.Slice(targets, func(left int, right int) bool {
		return targets[left].IP.String() < targets[right].IP.String()
	})
	return targets, nil
}
