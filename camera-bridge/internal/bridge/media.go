// Package bridge provides the local RTSP fan-out for verified PLAF203 H.264.
package bridge

import (
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"strings"
	"sync"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/aac"
	"github.com/AlexxIT/go2rtc/pkg/core"
	"github.com/AlexxIT/go2rtc/pkg/h264/annexb"
	"github.com/AlexxIT/go2rtc/pkg/rtsp"
	"github.com/aerodomigue/petlibro-camera-bridge/internal/plaf203"
	"github.com/pion/rtp"
)

const (
	mediaPathPrefix       = "/device/"
	defaultMediaListen    = ":8554"
	mediaClockRate        = 90_000
	mediaTimestampDivisor = time.Second
	confirmedAACADTS      = "\xff\xf1\x50\x40\x10\x61\x10"
)

// MediaServer serves device-scoped RTSP paths to the internal go2rtc
// producer. It is intentionally unauthenticated because it is bound only to
// the local Docker host network and never exposed by the relay dashboard.
type MediaServer struct {
	listener  net.Listener
	registry  *Registry
	closeOnce sync.Once
}

// StartMediaServer starts the bridge-owned RTSP endpoint.
func StartMediaServer(listenAddress string, registry *Registry) (*MediaServer, error) {
	if registry == nil {
		return nil, errors.New("camera registry is required")
	}
	if listenAddress == "" {
		listenAddress = defaultMediaListen
	}
	listener, err := net.Listen("tcp", listenAddress)
	if err != nil {
		return nil, fmt.Errorf("listen camera media RTSP: %w", err)
	}
	server := &MediaServer{listener: listener, registry: registry}
	log.Printf("CAMERA MEDIA SERVER START address=%s", listener.Addr())
	go server.acceptLoop()
	return server, nil
}

// Close stops new RTSP consumers. Existing accepted connections are closed by
// their owning RTSP sessions when their TCP peer goes away.
func (server *MediaServer) Close() error {
	if server == nil || server.listener == nil {
		return nil
	}
	var closeErr error
	server.closeOnce.Do(func() {
		closeErr = server.listener.Close()
	})
	return closeErr
}

func (server *MediaServer) acceptLoop() {
	for {
		connection, err := server.listener.Accept()
		if err != nil {
			if !errors.Is(err, net.ErrClosed) {
				log.Printf("CAMERA MEDIA SERVER ACCEPT FAILED error=%v", err)
			}
			return
		}
		go server.handleConnection(connection)
	}
}

func (server *MediaServer) handleConnection(connection net.Conn) {
	consumer := rtsp.NewServer(connection)
	var release func()
	consumer.Listen(func(message any) {
		if message != rtsp.MethodDescribe || consumer.URL == nil || release != nil {
			return
		}
		deviceID, ok := deviceIDFromMediaPath(consumer.URL.Path)
		if !ok {
			return
		}
		closer, err := server.registry.addMediaConsumer(deviceID, consumer)
		if err != nil {
			log.Printf("CAMERA MEDIA CLIENT REJECTED device=%s peer=%s error=%v", deviceID, connection.RemoteAddr(), err)
			return
		}
		release = closer
		log.Printf("CAMERA MEDIA CLIENT CONNECTED device=%s peer=%s", deviceID, connection.RemoteAddr())
	})
	if err := consumer.Accept(); err != nil {
		if !errors.Is(err, io.EOF) {
			log.Printf("CAMERA MEDIA CLIENT SETUP FAILED peer=%s error=%v", connection.RemoteAddr(), err)
		}
	} else if release != nil {
		// rtsp.Conn.Accept returns as soon as the consumer has completed PLAY.
		// The connection must remain in Handle for RTP output and RTSP keepalives;
		// closing it here makes go2rtc retry the source in a tight EOF loop.
		if err := consumer.Handle(); err != nil && !errors.Is(err, io.EOF) {
			log.Printf("CAMERA MEDIA CLIENT HANDLE FAILED peer=%s error=%v", connection.RemoteAddr(), err)
		}
	}
	_ = consumer.Stop()
	if release != nil {
		release()
	}
}

func deviceIDFromMediaPath(path string) (string, bool) {
	if !strings.HasPrefix(path, mediaPathPrefix) {
		return "", false
	}
	deviceID := strings.TrimPrefix(path, mediaPathPrefix)
	if !validDeviceID(deviceID) {
		return "", false
	}
	return deviceID, true
}

type mediaPublisher struct {
	videoTrack  *core.Receiver
	videoMedia  *core.Media
	videoCodec  *core.Codec
	audioTrack  *core.Receiver
	audioMedia  *core.Media
	audioCodec  *core.Codec
	startedAt   time.Time
	latestFrame *plaf203.VideoFrame
	mu          sync.Mutex
}

func newMediaPublisher() *mediaPublisher {
	videoCodec := &core.Codec{
		Name:        core.CodecH264,
		ClockRate:   mediaClockRate,
		PayloadType: core.PayloadTypeRAW,
	}
	videoMedia := &core.Media{
		Kind:      core.KindVideo,
		Direction: core.DirectionRecvonly,
		Codecs:    []*core.Codec{videoCodec},
	}
	audioCodec := aac.ADTSToCodec([]byte(confirmedAACADTS))
	if audioCodec == nil {
		panic("confirmed PLAF203 AAC ADTS header is invalid")
	}
	audioMedia := &core.Media{
		Kind:      core.KindAudio,
		Direction: core.DirectionRecvonly,
		Codecs:    []*core.Codec{audioCodec},
	}
	return &mediaPublisher{
		videoTrack: core.NewReceiver(videoMedia, videoCodec),
		videoMedia: videoMedia,
		videoCodec: videoCodec,
		audioTrack: core.NewReceiver(audioMedia, audioCodec),
		audioMedia: audioMedia,
		audioCodec: audioCodec,
		startedAt:  time.Now(),
	}
}

func (publisher *mediaPublisher) publish(frame *plaf203.VideoFrame) {
	if publisher == nil || frame == nil || len(frame.Data) == 0 {
		return
	}
	avcc := annexb.EncodeToAVCC(frame.Data)
	if len(avcc) == 0 {
		return
	}
	publisher.mu.Lock()
	if frame.Keyframe {
		publisher.latestFrame = cloneFrame(frame)
	}
	timestamp := uint32(time.Since(publisher.startedAt) * mediaClockRate / mediaTimestampDivisor)
	publisher.mu.Unlock()
	publisher.videoTrack.WriteRTP(&rtp.Packet{
		// Version zero marks AVCC for go2rtc's internal H264 packetizer. It
		// converts this payload into standards-compliant RTP before WebRTC.
		Header:  rtp.Header{Marker: true, Timestamp: timestamp},
		Payload: avcc,
	})
}

func (publisher *mediaPublisher) publishAudio(frame *plaf203.AudioFrame) {
	if publisher == nil || frame == nil || len(frame.Data) <= aac.ADTSHeaderSize {
		return
	}
	publisher.audioTrack.WriteRTP(&rtp.Packet{
		// Version zero marks one raw AAC access unit. The RTSP consumer applies
		// RFC 3640 packetization and preserves the 1024-sample AAC cadence.
		Header:  rtp.Header{Marker: true},
		Payload: append([]byte(nil), frame.Data[aac.ADTSHeaderSize:]...),
	})
}

func (publisher *mediaPublisher) attach(consumer *rtsp.Conn) error {
	if publisher == nil || consumer == nil {
		return errors.New("camera media consumer is unavailable")
	}
	if err := consumer.AddTrack(publisher.videoMedia, publisher.videoCodec, publisher.videoTrack); err != nil {
		return err
	}
	if err := consumer.AddTrack(publisher.audioMedia, publisher.audioCodec, publisher.audioTrack); err != nil {
		return err
	}
	publisher.mu.Lock()
	frame := cloneFrame(publisher.latestFrame)
	publisher.mu.Unlock()
	if frame != nil {
		publisher.publish(frame)
	}
	return nil
}

func cloneFrame(frame *plaf203.VideoFrame) *plaf203.VideoFrame {
	if frame == nil {
		return nil
	}
	clone := *frame
	clone.Data = append([]byte(nil), frame.Data...)
	return &clone
}
