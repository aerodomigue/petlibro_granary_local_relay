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

	lanSearchLength = 88
	knockLength     = 52

	clientMagicVersion = 0x1C
	deviceMagicVersion = 0x1B
	controlFlags       = 0x02

	lanSearchOpcode  uint16 = 0x0601
	knockOpcode      uint16 = 0x0402
	knockReplyOpcode uint16 = 0x0404

	lanSearchSubtype uint16 = 0x0021
	knockSubtype     uint16 = 0x0033

	uidOffset   = 16
	nonceOffset = 36

	lanSearchSDKOffset = 52
	lanSearchNonce     = 56
	lanSearchPhase     = 64
	lanSearchText      = 74
	knockSDKOffset     = 48
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

// Knock2 is the fixed-width identity correlation emitted by the feeder after
// LAN_SEARCH3. The source address is supplied by the transport, not the packet.
type Knock2 struct {
	UID        string
	Nonce      [8]byte
	SDKVersion [4]byte
}

// KnockReply is the fixed-width KNOCK_RR2 acknowledgement sent to a verified
// KNOCK2 source address.
type KnockReply struct {
	UID   string
	Nonce [8]byte
}

// EncodeLANSearch3 builds the fixed-width, plaintext LAN_SEARCH3 payload.
// Callers must apply the official TUTK wire transform before UDP transmission.
func EncodeLANSearch3(uid string, nonce [8]byte) ([]byte, error) {
	return (LANSearch3{UID: uid, Nonce: nonce, Phase: 1}).Encode()
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

// DecodeKnock2 validates a decrypted feeder KNOCK2 datagram and extracts its
// UID and nonce. The UID check is mandatory before an address becomes a device
// candidate.
func DecodeKnock2(packet []byte, expectedUID string, expectedNonce [8]byte) (Knock2, error) {
	if err := ValidateUID(expectedUID); err != nil {
		return Knock2{}, err
	}
	if len(packet) != knockLength {
		return Knock2{}, fmt.Errorf("%w: got=%d want=%d", ErrPacketTooShort, len(packet), knockLength)
	}
	if packet[0] != 0x04 || packet[1] != 0x02 || packet[2] != deviceMagicVersion || packet[3] != controlFlags {
		return Knock2{}, ErrUnexpectedPacket
	}
	if binary.LittleEndian.Uint16(packet[4:]) != knockLength-16 ||
		binary.LittleEndian.Uint16(packet[8:]) != knockOpcode ||
		binary.LittleEndian.Uint16(packet[10:]) != knockSubtype {
		return Knock2{}, ErrUnexpectedPacket
	}

	knock := Knock2{UID: string(packet[uidOffset : uidOffset+UIDLength])}
	copy(knock.Nonce[:], packet[nonceOffset:nonceOffset+len(knock.Nonce)])
	copy(knock.SDKVersion[:], packet[knockSDKOffset:knockSDKOffset+len(knock.SDKVersion)])
	if knock.UID != expectedUID {
		return Knock2{}, ErrUIDMismatch
	}
	if knock.Nonce != expectedNonce {
		return Knock2{}, ErrUnexpectedPacket
	}
	return knock, nil
}

// EncodeKnockReply builds the KNOCK_RR2 acknowledgement for a verified KNOCK2.
func EncodeKnockReply(uid string, nonce [8]byte) ([]byte, error) {
	return (KnockReply{UID: uid, Nonce: nonce}).Encode()
}

// Encode serializes a fixed-width KNOCK_RR2 acknowledgement before wire encryption.
func (reply KnockReply) Encode() ([]byte, error) {
	if err := ValidateUID(reply.UID); err != nil {
		return nil, err
	}
	packet := make([]byte, knockLength)
	packet[0] = 0x04
	packet[1] = 0x02
	packet[2] = clientMagicVersion
	packet[3] = controlFlags
	binary.LittleEndian.PutUint16(packet[4:], knockLength-16)
	binary.LittleEndian.PutUint16(packet[8:], knockReplyOpcode)
	binary.LittleEndian.PutUint16(packet[10:], knockSubtype)
	copy(packet[uidOffset:uidOffset+UIDLength], reply.UID)
	copy(packet[nonceOffset:nonceOffset+len(reply.Nonce)], reply.Nonce[:])
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
