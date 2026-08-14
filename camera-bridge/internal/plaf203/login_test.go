package plaf203

import (
	"encoding/binary"
	"encoding/hex"
	"errors"
	"os"
	"strings"
	"testing"

	"github.com/AlexxIT/go2rtc/pkg/tutk"
)

func TestDecodeLoginResponseFromCapturedWireFixture(t *testing.T) {
	wirePayload, err := os.ReadFile("testdata/login_success_wire.hex")
	if err != nil {
		t.Fatal(err)
	}
	encodedPayload, err := hex.DecodeString(strings.TrimSpace(string(wirePayload)))
	if err != nil {
		t.Fatal(err)
	}
	expectedSessionID := [8]byte{0x8d, 0x3c, 0xf5, 0x0a, 0x9f, 0x26, 0xf8, 0xd8}
	response, err := DecodeLoginResponse(tutk.ReverseTransCodePartial(nil, encodedPayload), expectedSessionID)
	if err != nil {
		t.Fatal(err)
	}
	if response.SessionID != expectedSessionID || response.Sequence != 0 {
		t.Fatalf("unexpected login response: %+v", response)
	}
}

func TestDecodeLoginResponseRejectsInvalidPackets(t *testing.T) {
	sessionID := [8]byte{1, 2, 3, 4, 5, 6, 7, 8}
	packet := testLoginSuccess(sessionID)

	t.Run("truncated", func(t *testing.T) {
		if _, err := DecodeLoginResponse(packet[:loginResponseLength-1], sessionID); !errors.Is(err, ErrPacketTooShort) {
			t.Fatalf("error=%v", err)
		}
	})
	t.Run("invalid magic", func(t *testing.T) {
		invalid := append([]byte(nil), packet...)
		invalid[2] = clientSessionMagicVersion
		if _, err := DecodeLoginResponse(invalid, sessionID); !errors.Is(err, ErrUnexpectedPacket) {
			t.Fatalf("error=%v", err)
		}
	})
	t.Run("invalid opcode", func(t *testing.T) {
		invalid := append([]byte(nil), packet...)
		binary.LittleEndian.PutUint16(invalid[8:], clientSessionOpcode)
		if _, err := DecodeLoginResponse(invalid, sessionID); !errors.Is(err, ErrUnexpectedPacket) {
			t.Fatalf("error=%v", err)
		}
	})
	t.Run("rejected acknowledgement", func(t *testing.T) {
		invalid := append([]byte(nil), packet...)
		invalid[sessionHeaderLength+1] = 0
		if _, err := DecodeLoginResponse(invalid, sessionID); !errors.Is(err, ErrLoginRejected) {
			t.Fatalf("error=%v", err)
		}
	})
}

func TestLoginRequestEncodeMatchesCapturedLayout(t *testing.T) {
	sessionID := [8]byte{0x8d, 0x3c, 0xf5, 0x0a, 0x9f, 0x26, 0xf8, 0xd8}
	request := LoginRequest{
		SessionID:       sessionID,
		Sequence:        1,
		Variant:         LoginSecondary,
		TimestampMillis: 1_786_544_102,
	}
	packet, err := request.Encode()
	if err != nil {
		t.Fatal(err)
	}
	if len(packet) != loginRequestLength || packet[0] != 0x04 || packet[1] != 0x02 || packet[2] != clientSessionMagicVersion || packet[3] != sessionFlags {
		t.Fatalf("unexpected login header: %x", packet[:4])
	}
	if binary.LittleEndian.Uint16(packet[4:]) != loginRequestLength-16 || binary.LittleEndian.Uint16(packet[6:]) != request.Sequence || binary.LittleEndian.Uint16(packet[8:]) != clientSessionOpcode || binary.LittleEndian.Uint16(packet[10:]) != clientSessionSubtype {
		t.Fatalf("unexpected login envelope")
	}
	if string(packet[12:28]) != string(sessionID16(sessionID)) {
		t.Fatal("login session ID does not match the captured Session16 layout")
	}
	command := packet[sessionHeaderLength:]
	if command[0] != 0 || command[1] != 0x20 || command[2] != loginCommandVersion || binary.LittleEndian.Uint16(command[16:]) != loginRequestLength-52 || binary.LittleEndian.Uint32(command[20:]) != request.TimestampMillis {
		t.Fatalf("unexpected login command: %x", command[:loginCommandLength])
	}
	payload := command[loginCommandLength:]
	if string(payload[:len(loginUsername)]) != loginUsername || string(payload[loginFieldLength:loginFieldLength+len(loginPassword)]) != loginPassword {
		t.Fatal("login fields do not match the captured V3.0.30 layout")
	}
	config := payload[2*loginFieldLength:]
	if config[0] != loginConfigMode || binary.LittleEndian.Uint32(config[4:]) != loginConfigValue || string(config[8:12]) != string(loginCapabilityBitmap[:]) || config[22] != 3 || config[28] != 1 {
		t.Fatalf("unexpected fixed login configuration: %x", config)
	}
}

func TestLoginRequestEncodeRejectsUnknownVariant(t *testing.T) {
	if _, err := (LoginRequest{Variant: LoginVariant(99)}).Encode(); err == nil {
		t.Fatal("expected an invalid variant error")
	}
}

func testLoginSuccess(sessionID [8]byte) []byte {
	packet := make([]byte, loginResponseLength)
	packet[0] = 0x04
	packet[1] = 0x02
	packet[2] = deviceSessionMagicVersion
	packet[3] = sessionFlags
	binary.LittleEndian.PutUint16(packet[4:], loginResponseLength-16)
	binary.LittleEndian.PutUint16(packet[8:], deviceSessionOpcode)
	binary.LittleEndian.PutUint16(packet[10:], deviceLoginSubtype)
	copy(packet[12:28], sessionID16(sessionID))
	packet[sessionHeaderLength] = 0
	packet[sessionHeaderLength+1] = loginSuccessCommand
	packet[sessionHeaderLength+2] = loginCommandVersion
	return packet
}
