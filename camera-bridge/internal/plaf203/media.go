package plaf203

import (
	"encoding/binary"
	"errors"
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
	aacChannel            uint8 = 0x03
	aacCodec                    = "aac-lc"
	aacSampleRate               = 44_100
	aacChannelCount             = 1
	aacAccessUnitSamples        = 1_024
	adtsHeaderLength            = 7
)

// VideoFrame is a fully assembled H.264 access unit. It intentionally does
// not perform decoding, transcoding, or network publication.
type VideoFrame struct {
	Codec     string
	Timestamp uint64
	Keyframe  bool
	Data      []byte
}

// AudioFrame is a fully assembled AAC-LC ADTS access unit. The data includes
// its ADTS header so the downstream RTSP publisher can derive the codec safely.
type AudioFrame struct {
	Codec      string
	SampleRate int
	Channels   int
	Samples    int
	Timestamp  uint64
	Data       []byte
}

// MediaStats is safe diagnostic state for the bridge API. It never exposes
// frame payloads.
type MediaStats struct {
	VideoCodec          string
	AudioCodec          string
	FramesReceived      uint64
	BytesReceived       uint64
	LastFrameAt         time.Time
	AudioFramesReceived uint64
	AudioBytesReceived  uint64
	LastAudioAt         time.Time
}

// MediaObservation describes a non-video media packet without retaining its
// payload. It is used only for rate-limited codec discovery diagnostics.
type MediaObservation struct {
	ChannelID     uint16
	PayloadLength int
	FrameNumber   uint32
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

type mediaFragment struct {
	channelID     uint16
	channel       uint8
	fragmentCount uint8
	payload       []byte
	subsequence   uint16
	frameNumber   uint32
	isEndFragment bool
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
	fragment, err := decodeMediaFragment(packet, expectedSessionID)
	if err != nil {
		return nil, err
	}
	if fragment == nil || (fragment.channel != h264MainChannel && fragment.channel != h264SubChannel) {
		return nil, nil
	}
	frameData, complete, err := receiver.assemble(fragment, now)
	if err != nil || !complete {
		return nil, err
	}
	frameData, timestamp := stripMediaMetadata(frameData)
	if !containsH264NAL(frameData) {
		return nil, fmt.Errorf("PLAF203 media frame does not contain an H.264 NAL")
	}
	frame := &VideoFrame{
		Codec:     "h264",
		Timestamp: timestamp,
		Keyframe:  containsH264IDR(frameData),
		Data:      frameData,
	}
	receiver.mu.Lock()
	receiver.stats.VideoCodec = frame.Codec
	receiver.stats.FramesReceived++
	receiver.stats.BytesReceived += uint64(len(frame.Data))
	receiver.stats.LastFrameAt = now.UTC()
	receiver.mu.Unlock()
	return frame, nil
}

// HandleAudioPacket parses one confirmed PLAF203 AAC-LC ADTS media frame.
// Audio uses channel 0x0103 and exactly one ADTS access unit plus 16 bytes of
// trailing media metadata in the official V3.0.30 capture.
func (receiver *MediaReceiver) HandleAudioPacket(packet []byte, expectedSessionID [8]byte, now time.Time) (*AudioFrame, error) {
	fragment, err := decodeMediaFragment(packet, expectedSessionID)
	if err != nil {
		return nil, err
	}
	if fragment == nil || fragment.channel != aacChannel {
		return nil, nil
	}
	frameData, complete, err := receiver.assemble(fragment, now)
	if err != nil || !complete {
		return nil, err
	}
	accessUnit, timestamp, err := parsePLAF203ADTS(frameData)
	if err != nil {
		return nil, err
	}
	frame := &AudioFrame{
		Codec:      aacCodec,
		SampleRate: aacSampleRate,
		Channels:   aacChannelCount,
		Samples:    aacAccessUnitSamples,
		Timestamp:  timestamp,
		Data:       accessUnit,
	}
	receiver.mu.Lock()
	receiver.stats.AudioCodec = frame.Codec
	receiver.stats.AudioFramesReceived++
	receiver.stats.AudioBytesReceived += uint64(len(frame.Data))
	receiver.stats.LastAudioAt = now.UTC()
	receiver.mu.Unlock()
	return frame, nil
}

// ObserveNonVideoPacket extracts safe metadata for a Session16 media packet
// that is not one of the two H.264 channels. It deliberately does not infer an
// audio codec from a channel number or retain any camera payload.
func (receiver *MediaReceiver) ObserveNonVideoPacket(packet []byte, expectedSessionID [8]byte) (*MediaObservation, error) {
	fragment, err := decodeMediaFragment(packet, expectedSessionID)
	if err != nil {
		return nil, err
	}
	if fragment == nil {
		return nil, nil
	}
	if fragment.channel == h264MainChannel || fragment.channel == h264SubChannel || fragment.channel == aacChannel {
		return nil, nil
	}
	return &MediaObservation{
		ChannelID:     fragment.channelID,
		PayloadLength: len(fragment.payload),
		FrameNumber:   fragment.frameNumber,
	}, nil
}

func decodeMediaFragment(packet []byte, expectedSessionID [8]byte) (*mediaFragment, error) {
	inner, err := decodeDeviceSession(packet, expectedSessionID)
	if err != nil {
		return nil, err
	}
	if len(inner) < mediaHeaderLength || inner[0] != 0x0C || inner[2] != loginCommandVersion {
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
	channelID := binary.LittleEndian.Uint16(inner[16:18])
	return &mediaFragment{
		channelID:     channelID,
		channel:       uint8(channelID),
		fragmentCount: fragmentCount,
		payload:       append([]byte(nil), inner[mediaHeaderLength:mediaHeaderLength+payloadLength]...),
		subsequence:   binary.LittleEndian.Uint16(inner[18:20]),
		frameNumber:   binary.LittleEndian.Uint32(inner[28:32]),
		isEndFragment: inner[1]&0x01 != 0 && channelID&0x0100 != 0,
	}, nil
}

func (receiver *MediaReceiver) assemble(fragment *mediaFragment, now time.Time) ([]byte, bool, error) {
	receiver.mu.Lock()
	defer receiver.mu.Unlock()
	receiver.expireLocked(now)
	if receiver.isDuplicateLocked(fragment.channel, fragment.subsequence) {
		return nil, false, nil
	}
	assembly := receiver.assemblies[fragment.channel]
	if assembly == nil || assembly.frameNumber != fragment.frameNumber {
		assembly = &mediaAssembly{
			frameNumber:       fragment.frameNumber,
			expectedFragments: fragment.fragmentCount,
			lastSubsequence:   fragment.subsequence,
			startedAt:         now,
			seenSubsequences:  make(map[uint16]struct{}),
		}
		receiver.assemblies[fragment.channel] = assembly
	}
	if assembly.expectedFragments != fragment.fragmentCount || len(assembly.data)+len(fragment.payload) > maxMediaFrameBytes {
		delete(receiver.assemblies, fragment.channel)
		return nil, false, fmt.Errorf("inconsistent PLAF203 media frame channel=%d frame=%d", fragment.channel, fragment.frameNumber)
	}
	if _, found := assembly.seenSubsequences[fragment.subsequence]; found {
		return nil, false, nil
	}
	assembly.seenSubsequences[fragment.subsequence] = struct{}{}
	assembly.data = append(assembly.data, fragment.payload...)
	assembly.receivedFragments++
	assembly.lastSubsequence = fragment.subsequence
	if fragment.fragmentCount != 1 && !fragment.isEndFragment {
		return nil, false, nil
	}
	defer delete(receiver.assemblies, fragment.channel)
	if assembly.receivedFragments != assembly.expectedFragments {
		return nil, false, fmt.Errorf("incomplete PLAF203 media frame channel=%d frame=%d got=%d want=%d", fragment.channel, fragment.frameNumber, assembly.receivedFragments, assembly.expectedFragments)
	}
	receiver.lastSubsequence[fragment.channel] = fragment.subsequence
	return append([]byte(nil), assembly.data...), true, nil
}

func parsePLAF203ADTS(data []byte) ([]byte, uint64, error) {
	if len(data) < adtsHeaderLength+mediaMetadataLength || data[0] != 0xFF || data[1]&0xF6 != 0xF0 || data[1]&0x01 == 0 {
		return nil, 0, errors.New("invalid PLAF203 AAC ADTS header")
	}
	profile := (data[2] >> 6) & 0x03
	sampleRateIndex := (data[2] >> 2) & 0x0F
	channels := ((data[2] & 0x01) << 2) | (data[3] >> 6)
	if profile != 1 || sampleRateIndex != 4 || channels != aacChannelCount {
		return nil, 0, fmt.Errorf("unsupported PLAF203 AAC ADTS config profile=%d sample_rate_index=%d channels=%d", profile, sampleRateIndex, channels)
	}
	frameLength := int(data[3]&0x03)<<11 | int(data[4])<<3 | int(data[5]>>5)
	if frameLength < adtsHeaderLength || len(data) != frameLength+mediaMetadataLength {
		return nil, 0, fmt.Errorf("invalid PLAF203 AAC frame length frame=%d payload=%d", frameLength, len(data))
	}
	timestamp := uint64(binary.LittleEndian.Uint32(data[len(data)-4:]))
	return append([]byte(nil), data[:frameLength]...), timestamp, nil
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
