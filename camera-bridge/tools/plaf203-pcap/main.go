// Command plaf203-pcap inspects captured PLAF203 UDP traffic offline.
package main

import (
	"encoding/binary"
	"encoding/hex"
	"flag"
	"fmt"
	"io"
	"os"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/tutk"
	"github.com/aerodomigue/petlibro-camera-bridge/internal/plaf203"
)

const (
	pcapHeaderLength   = 24
	pcapRecordLength   = 16
	sll2HeaderLength   = 20
	ethernetHeaderSize = 14
	udpProtocol        = 17
	minimumIPv4Header  = 20
	minimumUDPHeader   = 8
	decodedPreviewSize = 48
	sessionHeaderSize  = 28
	sessionOpcodeSend  = 0x0407
	sessionOpcodeRecv  = 0x0408
	mediaHeaderSize    = 36
)

type pcapRecord struct {
	timestamp time.Time
	frame     []byte
}

func main() {
	pcapPath := flag.String("pcap", "", "path to a classic libpcap file")
	deviceAddress := flag.String("device-ip", "", "optional feeder IPv4 address filter")
	directOnly := flag.Bool("direct-only", false, "exclude traffic that is not directly between the client and feeder")
	sessionOnly := flag.Bool("session-only", false, "show only decoded Session16 0x0407/0x0408 datagrams")
	full := flag.Bool("full", false, "include complete raw and decoded payload hex")
	flag.Parse()
	if *pcapPath == "" {
		fmt.Fprintln(os.Stderr, "-pcap is required")
		os.Exit(2)
	}
	if err := inspect(*pcapPath, *deviceAddress, *directOnly, *sessionOnly, *full); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func inspect(pcapPath string, deviceAddress string, directOnly bool, sessionOnly bool, full bool) error {
	file, err := os.Open(pcapPath)
	if err != nil {
		return fmt.Errorf("open pcap: %w", err)
	}
	defer func() { _ = file.Close() }()

	header := make([]byte, pcapHeaderLength)
	if _, err := io.ReadFull(file, header); err != nil {
		return fmt.Errorf("read pcap header: %w", err)
	}
	if binary.LittleEndian.Uint32(header[:4]) != 0xA1B2C3D4 {
		return fmt.Errorf("only little-endian microsecond libpcap files are supported")
	}
	linkType := binary.LittleEndian.Uint32(header[20:])
	for {
		record, readErr := readRecord(file)
		if errorsIsEOF(readErr) {
			return nil
		}
		if readErr != nil {
			return readErr
		}
		packet, ok := decodeUDP(linkType, record.frame)
		if !ok || (deviceAddress != "" && packet.source != deviceAddress && packet.destination != deviceAddress) {
			continue
		}
		if directOnly && (packet.sourcePort == 10001 || packet.destinationPort == 10001) {
			continue
		}
		decoded := tutk.ReverseTransCodePartial(nil, packet.payload)
		if sessionOnly && !isSessionDatagram(decoded) {
			continue
		}
		fmt.Printf("%s %s:%d -> %s:%d len=%d %s\n", record.timestamp.UTC().Format(time.RFC3339Nano), packet.source, packet.sourcePort, packet.destination, packet.destinationPort, len(packet.payload), packetSummary(decoded))
		if full {
			fmt.Printf("  raw=%s\n  decoded=%s\n", hex.EncodeToString(packet.payload), hex.EncodeToString(decoded))
		}
	}
}

func readRecord(reader io.Reader) (pcapRecord, error) {
	header := make([]byte, pcapRecordLength)
	if _, err := io.ReadFull(reader, header); err != nil {
		return pcapRecord{}, err
	}
	capturedLength := binary.LittleEndian.Uint32(header[8:])
	frame := make([]byte, capturedLength)
	if _, err := io.ReadFull(reader, frame); err != nil {
		return pcapRecord{}, fmt.Errorf("read pcap frame: %w", err)
	}
	return pcapRecord{
		timestamp: time.Unix(int64(binary.LittleEndian.Uint32(header[:4])), int64(binary.LittleEndian.Uint32(header[4:]))*int64(time.Microsecond)),
		frame:     frame,
	}, nil
}

type udpPacket struct {
	source          string
	destination     string
	sourcePort      uint16
	destinationPort uint16
	payload         []byte
}

func decodeUDP(linkType uint32, frame []byte) (udpPacket, bool) {
	ipOffset, ok := ipv4Offset(linkType, frame)
	if !ok || len(frame) < ipOffset+minimumIPv4Header {
		return udpPacket{}, false
	}
	ip := frame[ipOffset:]
	if ip[0]>>4 != 4 || ip[9] != udpProtocol {
		return udpPacket{}, false
	}
	ipHeaderLength := int(ip[0]&0x0F) * 4
	if ipHeaderLength < minimumIPv4Header || len(ip) < ipHeaderLength+minimumUDPHeader {
		return udpPacket{}, false
	}
	udp := ip[ipHeaderLength:]
	return udpPacket{
		source:          ipv4String(ip[12:16]),
		destination:     ipv4String(ip[16:20]),
		sourcePort:      binary.BigEndian.Uint16(udp[:2]),
		destinationPort: binary.BigEndian.Uint16(udp[2:4]),
		payload:         append([]byte(nil), udp[minimumUDPHeader:]...),
	}, true
}

func ipv4Offset(linkType uint32, frame []byte) (int, bool) {
	switch linkType {
	case 276: // DLT_LINUX_SLL2
		if len(frame) < sll2HeaderLength || binary.BigEndian.Uint16(frame[:2]) != 0x0800 {
			return 0, false
		}
		return sll2HeaderLength, true
	case 1: // DLT_EN10MB
		if len(frame) < ethernetHeaderSize || binary.BigEndian.Uint16(frame[12:14]) != 0x0800 {
			return 0, false
		}
		return ethernetHeaderSize, true
	default:
		return 0, false
	}
}

func packetSummary(decoded []byte) string {
	previewLength := len(decoded)
	if previewLength > decodedPreviewSize {
		previewLength = decodedPreviewSize
	}
	summary := fmt.Sprintf("decoded=%s", hex.EncodeToString(decoded[:previewLength]))
	if len(decoded) >= 12 && decoded[0] == 0x04 && decoded[1] == 0x02 {
		opcode := binary.LittleEndian.Uint16(decoded[8:10])
		summary += fmt.Sprintf(" magic=0x%02x flags=0x%02x seq=%d opcode=0x%04x", decoded[2], decoded[3], binary.LittleEndian.Uint16(decoded[6:8]), opcode)
		if opcode == 0x0602 {
			summary += lanSearchResponseSummary(decoded)
		}
		if isSessionDatagram(decoded) {
			summary += sessionSummary(decoded, opcode)
		}
	}
	return summary
}

func lanSearchResponseSummary(decoded []byte) string {
	const (
		lanSearchResponseUIDOffset = 16
		lanSearchResponseUIDLength = 20
	)
	if len(decoded) < lanSearchResponseUIDOffset+lanSearchResponseUIDLength {
		return " lan_search_r=truncated"
	}
	uid := string(decoded[lanSearchResponseUIDOffset : lanSearchResponseUIDOffset+lanSearchResponseUIDLength])
	response, err := plaf203.DecodeLANSearchResponse(decoded, uid)
	if err != nil {
		return fmt.Sprintf(" lan_search_r=invalid error=%v", err)
	}
	return fmt.Sprintf(" lan_search_r uid=%q uid_offset=16..35 nonce=unconfirmed endpoint=udp_source tail_marker=0x%08x token_offset=188..195 token=%s flags=0x%08x", response.UID, response.TailMarker, hex.EncodeToString(response.OpaqueToken[:]), response.ResponseFlags)
}

func isSessionDatagram(decoded []byte) bool {
	if len(decoded) < sessionHeaderSize || decoded[0] != 0x04 || decoded[1] != 0x02 {
		return false
	}
	opcode := binary.LittleEndian.Uint16(decoded[8:10])
	return opcode == sessionOpcodeSend || opcode == sessionOpcodeRecv
}

func sessionSummary(decoded []byte, opcode uint16) string {
	inner := decoded[sessionHeaderSize:]
	if len(inner) < 4 {
		return " inner=truncated"
	}
	summary := fmt.Sprintf(" inner=%02x%02x%02x%02x", inner[0], inner[1], inner[2], inner[3])
	if len(inner) >= mediaHeaderSize && inner[0] == 0x0C {
		payloadLength := binary.LittleEndian.Uint16(inner[24:26])
		channelID := binary.LittleEndian.Uint16(inner[16:18])
		summary += fmt.Sprintf(" channel=0x%04x sub=0x%04x fragments=%d index=%d payload=%d frame=%d", channelID, binary.LittleEndian.Uint16(inner[18:20]), inner[20], binary.LittleEndian.Uint16(inner[22:24]), payloadLength, binary.LittleEndian.Uint32(inner[28:32]))
		if payloadLength > 0 && mediaHeaderSize+int(payloadLength) <= len(inner) {
			payload := inner[mediaHeaderSize : mediaHeaderSize+payloadLength]
			if opcode == sessionOpcodeRecv {
				summary += h264Summary(payload)
			} else {
				summary += fmt.Sprintf(" payload_prefix=%s", hex.EncodeToString(payload[:min(len(payload), 12)]))
			}
		}
	}
	if opcode == sessionOpcodeRecv && len(inner) >= 3 && inner[0] == 0 && inner[1] == 0x21 && inner[2] == 0x0B {
		summary += " login_ack"
	}
	return summary
}

func min(left int, right int) int {
	if left < right {
		return left
	}
	return right
}

func h264Summary(payload []byte) string {
	for index := 0; index+4 < len(payload); index++ {
		if payload[index] == 0 && payload[index+1] == 0 && payload[index+2] == 0 && payload[index+3] == 1 {
			nalType := payload[index+4] & 0x1F
			return fmt.Sprintf(" h264_nal=%d", nalType)
		}
	}
	return ""
}

func ipv4String(address []byte) string {
	return fmt.Sprintf("%d.%d.%d.%d", address[0], address[1], address[2], address[3])
}

func errorsIsEOF(err error) bool {
	return err == io.EOF
}
