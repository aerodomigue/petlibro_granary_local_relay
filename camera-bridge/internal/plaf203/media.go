package plaf203

import (
	"encoding/binary"
	"fmt"
	"sync"
	"time"
)

const (
	mediaHeaderLength           = 36
	mediaMetadataLength         = 16
	maxMediaFrameBytes          = 2 * 1024 * 1024
	maxMediaFragmentCount       = 255
	mediaAssemblyTimeout        = time.Second
	h264CodecMarker             = 0x4E
	h264MainChannel       uint8 = 0x05
	h264SubChannel        uint8 = 0x07
)

// VideoFrame is a fully assembled H.264 access unit. It intentionally does
// not perform decoding, transcoding, or network publication.
type VideoFrame struct {
	Codec     string
	Timestamp uint64
	Keyframe  bool
	Data      []byte
}

// MediaStats is safe diagnostic state for the bridge API. It never exposes
// frame payloads.
type MediaStats struct {
	VideoCodec     string
	AudioCodec     string
	FramesReceived uint64
	BytesReceived  uint64
	LastFrameAt    time.Time
}

type mediaAssembly struct {
	frameNumber       uint32
	expectedFragments uint8
	receivedFragments uint8
	lastSubsequence   uint16
	startedAt         time.Time
	data              []byte
	seenSubsequences  map[uint16]struct{}
}

// MediaReceiver parses the V3.0.30 video fragment layout observed after the
// confirmed bootstrap. One receiver is owned by exactly one Session.
type MediaReceiver struct {
	mu              sync.Mutex
	assemblies      map[uint8]*mediaAssembly
	lastSubsequence map[uint8]uint16
	stats           MediaStats
}

// NewMediaReceiver creates isolated, bounded H.264 fragment state.
func NewMediaReceiver() *MediaReceiver {
	return &MediaReceiver{
		assemblies:      make(map[uint8]*mediaAssembly),
		lastSubsequence: make(map[uint8]uint16),
	}
}

// HandlePacket parses one decrypted feeder Session16 packet. It returns nil
// for valid non-media traffic and rejects malformed media safely.
func (receiver *MediaReceiver) HandlePacket(packet []byte, expectedSessionID [8]byte, now time.Time) (*VideoFrame, error) {
	inner, err := decodeDeviceSession(packet, expectedSessionID)
	if err != nil {
		return nil, err
	}
	if len(inner) < mediaHeaderLength || inner[0] != 0x0C || inner[2] != loginCommandVersion {
		return nil, nil
	}
	channelID := binary.LittleEndian.Uint16(inner[16:18])
	channel := uint8(channelID)
	if channel != h264MainChannel && channel != h264SubChannel {
		return nil, nil
	}
	fragmentCount := inner[20]
	if fragmentCount == 0 || fragmentCount > maxMediaFragmentCount {
		return nil, fmt.Errorf("invalid PLAF203 media fragment count %d", fragmentCount)
	}
	payloadLength := int(binary.LittleEndian.Uint16(inner[24:26]))
	if payloadLength == 0 || mediaHeaderLength+payloadLength > len(inner) {
		return nil, fmt.Errorf("invalid PLAF203 media payload length %d", payloadLength)
	}
	payload := append([]byte(nil), inner[mediaHeaderLength:mediaHeaderLength+payloadLength]...)
	subsequence := binary.LittleEndian.Uint16(inner[18:20])
	frameNumber := binary.LittleEndian.Uint32(inner[28:32])
	// V3.0.30 marks the terminal fragment with the low flag bit while retaining
	// additional flags (observed as 0x05), and sets the high channel byte.
	// Checking equality to 0x01 drops real terminal fragments such as 0x0c05
	// on channel 0x0105 before their SPS/PPS/IDR access unit can assemble.
	isEndFragment := inner[1]&0x01 != 0 && channelID&0x0100 != 0

	receiver.mu.Lock()
	defer receiver.mu.Unlock()
	receiver.expireLocked(now)
	if receiver.isDuplicateLocked(channel, subsequence) {
		return nil, nil
	}
	assembly := receiver.assemblies[channel]
	if assembly == nil || assembly.frameNumber != frameNumber {
		assembly = &mediaAssembly{
			frameNumber:       frameNumber,
			expectedFragments: fragmentCount,
			lastSubsequence:   subsequence,
			startedAt:         now,
			seenSubsequences:  make(map[uint16]struct{}),
		}
		receiver.assemblies[channel] = assembly
	}
	if assembly.expectedFragments != fragmentCount || len(assembly.data)+len(payload) > maxMediaFrameBytes {
		delete(receiver.assemblies, channel)
		return nil, fmt.Errorf("inconsistent PLAF203 media frame channel=%d frame=%d", channel, frameNumber)
	}
	if _, found := assembly.seenSubsequences[subsequence]; found {
		return nil, nil
	}
	assembly.seenSubsequences[subsequence] = struct{}{}
	assembly.data = append(assembly.data, payload...)
	assembly.receivedFragments++
	assembly.lastSubsequence = subsequence

	isComplete := fragmentCount == 1 || isEndFragment
	if !isComplete {
		return nil, nil
	}
	defer delete(receiver.assemblies, channel)
	if assembly.receivedFragments != assembly.expectedFragments {
		return nil, fmt.Errorf("incomplete PLAF203 media frame channel=%d frame=%d got=%d want=%d", channel, frameNumber, assembly.receivedFragments, assembly.expectedFragments)
	}
	frameData, timestamp := stripMediaMetadata(assembly.data)
	if !containsH264NAL(frameData) {
		return nil, fmt.Errorf("PLAF203 media frame does not contain an H.264 NAL")
	}
	frame := &VideoFrame{
		Codec:     "h264",
		Timestamp: timestamp,
		Keyframe:  containsH264IDR(frameData),
		Data:      frameData,
	}
	receiver.lastSubsequence[channel] = subsequence
	receiver.stats.VideoCodec = frame.Codec
	receiver.stats.FramesReceived++
	receiver.stats.BytesReceived += uint64(len(frame.Data))
	receiver.stats.LastFrameAt = now.UTC()
	return frame, nil
}

// Snapshot returns immutable diagnostic counters for one session.
func (receiver *MediaReceiver) Snapshot() MediaStats {
	receiver.mu.Lock()
	defer receiver.mu.Unlock()
	return receiver.stats
}

func (receiver *MediaReceiver) expireLocked(now time.Time) {
	for channel, assembly := range receiver.assemblies {
		if now.Sub(assembly.startedAt) > mediaAssemblyTimeout {
			delete(receiver.assemblies, channel)
		}
	}
}

func (receiver *MediaReceiver) isDuplicateLocked(channel uint8, subsequence uint16) bool {
	last, found := receiver.lastSubsequence[channel]
	if !found {
		return false
	}
	return subsequence == last || (subsequence < last && last-subsequence < 0x8000)
}

func stripMediaMetadata(data []byte) ([]byte, uint64) {
	if len(data) < mediaMetadataLength || data[len(data)-mediaMetadataLength] != h264CodecMarker || data[len(data)-mediaMetadataLength+1] != 0 {
		return data, 0
	}
	timestampOffset := len(data) - 4
	timestamp := uint64(binary.LittleEndian.Uint32(data[timestampOffset:]))
	return append([]byte(nil), data[:len(data)-mediaMetadataLength]...), timestamp
}

func containsH264NAL(data []byte) bool {
	return findH264NAL(data, -1)
}

func containsH264IDR(data []byte) bool {
	return findH264NAL(data, 5)
}

func findH264NAL(data []byte, wantedType int) bool {
	for index := 0; index+4 < len(data); index++ {
		startCodeLength := 0
		if data[index] == 0 && data[index+1] == 0 && data[index+2] == 1 {
			startCodeLength = 3
		} else if index+4 < len(data) && data[index] == 0 && data[index+1] == 0 && data[index+2] == 0 && data[index+3] == 1 {
			startCodeLength = 4
		}
		if startCodeLength == 0 || index+startCodeLength >= len(data) {
			continue
		}
		nalType := int(data[index+startCodeLength] & 0x1F)
		if wantedType < 0 || nalType == wantedType {
			return true
		}
	}
	return false
}
