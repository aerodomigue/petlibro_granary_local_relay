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
)

type pcapRecord struct {
	timestamp time.Time
	frame     []byte
}

func main() {
	pcapPath := flag.String("pcap", "", "path to a classic libpcap file")
	deviceAddress := flag.String("device-ip", "", "optional feeder IPv4 address filter")
	full := flag.Bool("full", false, "include complete raw and decoded payload hex")
	flag.Parse()
	if *pcapPath == "" {
		fmt.Fprintln(os.Stderr, "-pcap is required")
		os.Exit(2)
	}
	if err := inspect(*pcapPath, *deviceAddress, *full); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func inspect(pcapPath string, deviceAddress string, full bool) error {
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
		decoded := tutk.ReverseTransCodePartial(nil, packet.payload)
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
		summary += fmt.Sprintf(" magic=0x%02x flags=0x%02x opcode=0x%04x", decoded[2], decoded[3], binary.LittleEndian.Uint16(decoded[8:10]))
	}
	return summary
}

func ipv4String(address []byte) string {
	return fmt.Sprintf("%d.%d.%d.%d", address[0], address[1], address[2], address[3])
}

func errorsIsEOF(err error) bool {
	return err == io.EOF
}
