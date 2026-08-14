package plaf203

import (
	"context"
	"encoding/binary"
	"errors"
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

// Discover sends a UID-specific LAN_SEARCH3 broadcast probe and accepts only a
// LAN_SEARCH_R that proves the requested UID. The generated nonce is checked
// later against the KNOCK_RR2 response.
func Discover(ctx context.Context, transport DatagramTransport, uid string, nonce [8]byte, targets []*net.UDPAddr) (*net.UDPAddr, error) {
	return discover(ctx, transport, uid, nonce, targets, nil, nil)
}

// DiscoverUnicast sends LAN_SEARCH3 directly to one known feeder IPv4 address.
// A matching LAN_SEARCH_R must come back from that same IP, but may use a
// dynamic UDP source port as observed on the PLAF203.
func DiscoverUnicast(ctx context.Context, transport DatagramTransport, ip net.IP, uid string, nonce [8]byte) (*net.UDPAddr, error) {
	ipv4 := ip.To4()
	if ipv4 == nil || ipv4.IsUnspecified() {
		return nil, fmt.Errorf("PLAF203 discovery requires a valid feeder IPv4 address")
	}
	return discover(ctx, transport, uid, nonce, []*net.UDPAddr{{IP: ipv4, Port: LANPort}}, ipv4, nil)
}

func discover(ctx context.Context, transport DatagramTransport, uid string, nonce [8]byte, targets []*net.UDPAddr, expectedSourceIP net.IP, observe Observer) (*net.UDPAddr, error) {
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
			emitDiscoveryDiagnostic(observe, nil, "reject_missing_peer", len(packet), 0)
			continue
		}
		decoded := tutk.ReverseTransCodePartial(nil, packet)
		opcode := discoveryOpcode(decoded)
		emitDiscoveryDiagnostic(observe, address, "response", len(packet), opcode)
		if expectedSourceIP != nil && !address.IP.Equal(expectedSourceIP) {
			emitDiscoveryDiagnostic(observe, address, "reject_source_ip", len(packet), opcode)
			continue
		}
		if _, decodeErr := DecodeLANSearchResponse(decoded, uid); decodeErr != nil {
			emitDiscoveryDiagnostic(observe, address, discoveryRejectStep(decodeErr), len(packet), opcode)
			continue
		}
		emitDiscoveryDiagnostic(observe, address, "valid", len(packet), opcode)
		return &net.UDPAddr{IP: append(net.IP(nil), address.IP...), Port: address.Port}, nil
	}
}

// CompleteKnock sends LAN_SEARCH3 phase 2 then KNOCK2, and accepts only a
// KNOCK_RR2 with the requested UID and nonce. The feeder may change its UDP
// source port, so the peer IP is pinned while the port remains dynamic.
func CompleteKnock(ctx context.Context, transport DatagramTransport, address *net.UDPAddr, uid string, nonce [8]byte, observe Observer) (*net.UDPAddr, error) {
	if address == nil || address.IP == nil {
		return nil, fmt.Errorf("PLAF203 KNOCK requires a discovery peer")
	}
	phaseTwo, err := EncodeLANSearch3Phase(uid, nonce, 2)
	if err != nil {
		return nil, err
	}
	if err := transport.SendTo(tutk.TransCodePartial(nil, phaseTwo), address); err != nil {
		return nil, fmt.Errorf("send PLAF203 LAN_SEARCH3 phase 2: %w", err)
	}
	knock, err := EncodeKnock2(uid, nonce)
	if err != nil {
		return nil, err
	}
	if err := transport.SendTo(tutk.TransCodePartial(nil, knock), address); err != nil {
		return nil, fmt.Errorf("send PLAF203 KNOCK2: %w", err)
	}
	emit(observe, Event{State: StateKnocking, Address: address, Step: "wait"})
	for {
		packet, peer, receiveErr := transport.Receive(ctx)
		if receiveErr != nil {
			return nil, fmt.Errorf("receive PLAF203 KNOCK_RR2: %w", receiveErr)
		}
		if peer == nil || peer.IP == nil || !peer.IP.Equal(address.IP) {
			continue
		}
		decoded := tutk.ReverseTransCodePartial(nil, packet)
		if _, decodeErr := DecodeKnockReply(decoded, uid, nonce); decodeErr != nil {
			continue
		}
		return &net.UDPAddr{IP: append(net.IP(nil), peer.IP...), Port: peer.Port}, nil
	}
}

func emitDiscoveryDiagnostic(observe Observer, address *net.UDPAddr, step string, packetLength int, opcode uint16) {
	emit(observe, Event{
		State:        StateDiscovering,
		Address:      address,
		Step:         step,
		PacketLength: packetLength,
		Opcode:       opcode,
	})
}

func discoveryOpcode(packet []byte) uint16 {
	if len(packet) < 10 {
		return 0
	}
	return binary.LittleEndian.Uint16(packet[8:10])
}

func discoveryRejectStep(decodeErr error) string {
	if errors.Is(decodeErr, ErrUIDMismatch) {
		return "reject_uid_mismatch"
	}
	if errors.Is(decodeErr, ErrPacketTooShort) {
		return "reject_invalid_length"
	}
	return "reject_invalid_packet"
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
