// Package plaf203 implements the verified PLAF203 LAN connection preamble.
package plaf203

import (
	"encoding/binary"
	"errors"
	"fmt"
	"strings"
)

const (
	// UIDLength is the exact printable UID size reported by DEVICE_START_EVENT.
	UIDLength = 20
	// LANPort is the PLAF203 UDP discovery port.
	LANPort = 32761

	lanSearchLength         = 88
	lanSearchResponseLength = 200
	knockLength             = 52

	clientMagicVersion = 0x1C
	deviceMagicVersion = 0x1B
	controlFlags       = 0x02

	lanSearchOpcode         uint16 = 0x0601
	lanSearchResponseOpcode uint16 = 0x0602
	knockOpcode             uint16 = 0x0402
	knockReplyOpcode        uint16 = 0x0404

	lanSearchSubtype         uint16 = 0x0021
	lanSearchResponseSubtype uint16 = 0x0012
	knockSubtype             uint16 = 0x0033

	uidOffset   = 16
	nonceOffset = 36

	lanSearchSDKOffset = 52
	lanSearchNonce     = 56
	lanSearchPhase     = 64
	lanSearchText      = 74
	knockSDKOffset     = 48

	lanSearchResponseUIDOffset = 16
	lanSearchResponseTail      = 184
	lanSearchResponseToken     = 188
	lanSearchResponseFlags     = 196
)

var (
	// ErrInvalidUID rejects non-PLAF203 UID values before they reach the wire.
	ErrInvalidUID = errors.New("PLAF203 UID must be exactly 20 printable ASCII characters")
	// ErrPacketTooShort identifies malformed decoded datagrams.
	ErrPacketTooShort = errors.New("PLAF203 packet is truncated")
	// ErrUnexpectedPacket identifies a structurally valid but unrelated datagram.
	ErrUnexpectedPacket = errors.New("unexpected PLAF203 packet")
	// ErrUIDMismatch prevents accepting a discovery candidate for a different device.
	ErrUIDMismatch = errors.New("PLAF203 discovery UID does not match the requested device")
)

// clientSDKVersion is the opaque SDK value observed in the local PLAF203 PCAP.
var clientSDKVersion = [4]byte{0x02, 0x03, 0x03, 0x04}

// LANSearch3 is the fixed-width discovery request. The source address is
// intentionally absent because it is assigned by the UDP transport.
type LANSearch3 struct {
	UID   string
	Nonce [8]byte
	Phase byte
}

// LANSearchResponse is the fixed-width LAN_SEARCH_R response emitted after
// LAN_SEARCH3 phase 1. The UDP peer supplies the endpoint; the packet has no
// encoded UDP port. The opaque tail was captured from PLAF203 V3.0.30 but is
// not used for identity correlation.
type LANSearchResponse struct {
	UID           string
	TailMarker    uint32
	OpaqueToken   [8]byte
	ResponseFlags uint32
}

// KnockReply is the fixed-width KNOCK_RR2 identity correlation emitted after
// the client sends KNOCK2. The source address is supplied by the transport,
// not the packet.
type KnockReply struct {
	UID        string
	Nonce      [8]byte
	SDKVersion [4]byte
}

// EncodeLANSearch3 builds the fixed-width, plaintext LAN_SEARCH3 payload.
// Callers must apply the official TUTK wire transform before UDP transmission.
func EncodeLANSearch3(uid string, nonce [8]byte) ([]byte, error) {
	return (LANSearch3{UID: uid, Nonce: nonce, Phase: 1}).Encode()
}

// EncodeLANSearch3Phase builds LAN_SEARCH3 for an explicit protocol phase.
func EncodeLANSearch3Phase(uid string, nonce [8]byte, phase byte) ([]byte, error) {
	return (LANSearch3{UID: uid, Nonce: nonce, Phase: phase}).Encode()
}

// Encode serializes a fixed-width LAN_SEARCH3 packet before wire encryption.
func (search LANSearch3) Encode() ([]byte, error) {
	if err := ValidateUID(search.UID); err != nil {
		return nil, err
	}

	packet := make([]byte, lanSearchLength)
	packet[0] = 0x04
	packet[1] = 0x02
	packet[2] = clientMagicVersion
	packet[3] = controlFlags
	binary.LittleEndian.PutUint16(packet[4:], lanSearchLength-16)
	binary.LittleEndian.PutUint16(packet[8:], lanSearchOpcode)
	binary.LittleEndian.PutUint16(packet[10:], lanSearchSubtype)
	copy(packet[uidOffset:uidOffset+UIDLength], search.UID)
	copy(packet[lanSearchSDKOffset:lanSearchSDKOffset+len(clientSDKVersion)], clientSDKVersion[:])
	copy(packet[lanSearchNonce:lanSearchNonce+len(search.Nonce)], search.Nonce[:])
	packet[lanSearchPhase] = search.Phase
	copy(packet[lanSearchText:lanSearchText+8], "00000000")
	return packet, nil
}

// DecodeLANSearchResponse validates a decrypted LAN_SEARCH_R datagram. The
// feeder's dynamic UDP source is the endpoint. No field in the captured
// response has been confirmed as an echoed request nonce, so nonce validation
// happens on KNOCK_RR2.
func DecodeLANSearchResponse(packet []byte, expectedUID string) (LANSearchResponse, error) {
	if err := ValidateUID(expectedUID); err != nil {
		return LANSearchResponse{}, err
	}
	if len(packet) != lanSearchResponseLength {
		return LANSearchResponse{}, fmt.Errorf("%w: got=%d want=%d", ErrPacketTooShort, len(packet), lanSearchResponseLength)
	}
	if packet[0] != 0x04 || packet[1] != 0x02 || packet[2] != deviceMagicVersion || packet[3] != controlFlags {
		return LANSearchResponse{}, ErrUnexpectedPacket
	}
	if binary.LittleEndian.Uint16(packet[4:]) != lanSearchResponseLength-16 ||
		binary.LittleEndian.Uint16(packet[8:]) != lanSearchResponseOpcode ||
		binary.LittleEndian.Uint16(packet[10:]) != lanSearchResponseSubtype {
		return LANSearchResponse{}, ErrUnexpectedPacket
	}

	response := LANSearchResponse{UID: string(packet[lanSearchResponseUIDOffset : lanSearchResponseUIDOffset+UIDLength])}
	response.TailMarker = binary.LittleEndian.Uint32(packet[lanSearchResponseTail:])
	copy(response.OpaqueToken[:], packet[lanSearchResponseToken:lanSearchResponseToken+len(response.OpaqueToken)])
	response.ResponseFlags = binary.LittleEndian.Uint32(packet[lanSearchResponseFlags:])
	if response.UID != expectedUID {
		return LANSearchResponse{}, ErrUIDMismatch
	}
	return response, nil
}

// DecodeKnockReply validates a decrypted feeder KNOCK_RR2 datagram and
// extracts its UID and nonce. The UID and nonce checks are mandatory before
// the direct-LAN handshake can advance to LOGIN.
func DecodeKnockReply(packet []byte, expectedUID string, expectedNonce [8]byte) (KnockReply, error) {
	if err := ValidateUID(expectedUID); err != nil {
		return KnockReply{}, err
	}
	if len(packet) != knockLength {
		return KnockReply{}, fmt.Errorf("%w: got=%d want=%d", ErrPacketTooShort, len(packet), knockLength)
	}
	if packet[0] != 0x04 || packet[1] != 0x02 || packet[2] != deviceMagicVersion || packet[3] != controlFlags {
		return KnockReply{}, ErrUnexpectedPacket
	}
	if binary.LittleEndian.Uint16(packet[4:]) != knockLength-16 ||
		binary.LittleEndian.Uint16(packet[8:]) != knockReplyOpcode ||
		binary.LittleEndian.Uint16(packet[10:]) != knockSubtype {
		return KnockReply{}, ErrUnexpectedPacket
	}

	reply := KnockReply{UID: string(packet[uidOffset : uidOffset+UIDLength])}
	copy(reply.Nonce[:], packet[nonceOffset:nonceOffset+len(reply.Nonce)])
	copy(reply.SDKVersion[:], packet[knockSDKOffset:knockSDKOffset+len(reply.SDKVersion)])
	if reply.UID != expectedUID {
		return KnockReply{}, ErrUIDMismatch
	}
	if reply.Nonce != expectedNonce {
		return KnockReply{}, ErrUnexpectedPacket
	}
	return reply, nil
}

// EncodeKnock2 builds the client KNOCK2 packet sent after LAN_SEARCH_R.
func EncodeKnock2(uid string, nonce [8]byte) ([]byte, error) {
	return (Knock2{UID: uid, Nonce: nonce}).Encode()
}

// Knock2 is the fixed-width client KNOCK2 request.
type Knock2 struct {
	UID   string
	Nonce [8]byte
}

// Encode serializes a fixed-width KNOCK2 packet before wire encryption.
func (knock Knock2) Encode() ([]byte, error) {
	if err := ValidateUID(knock.UID); err != nil {
		return nil, err
	}
	packet := make([]byte, knockLength)
	packet[0] = 0x04
	packet[1] = 0x02
	packet[2] = clientMagicVersion
	packet[3] = controlFlags
	binary.LittleEndian.PutUint16(packet[4:], knockLength-16)
	binary.LittleEndian.PutUint16(packet[8:], knockOpcode)
	binary.LittleEndian.PutUint16(packet[10:], knockSubtype)
	copy(packet[uidOffset:uidOffset+UIDLength], knock.UID)
	copy(packet[nonceOffset:nonceOffset+len(knock.Nonce)], knock.Nonce[:])
	copy(packet[knockSDKOffset:knockSDKOffset+len(clientSDKVersion)], clientSDKVersion[:])
	return packet, nil
}

// ValidateUID confirms the discovered UID can be safely encoded as protocol bytes.
func ValidateUID(uid string) error {
	if len(uid) != UIDLength || strings.IndexFunc(uid, func(character rune) bool {
		return character < 0x21 || character > 0x7e
	}) != -1 {
		return ErrInvalidUID
	}
	return nil
}
