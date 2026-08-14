// Package bridge provides the local RTSP fan-out for verified PLAF203 H.264.
package bridge

import (
	"errors"
	"fmt"
	"log"
	"net"
	"strings"
	"sync"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/core"
	"github.com/AlexxIT/go2rtc/pkg/rtsp"
	"github.com/aerodomigue/petlibro-camera-bridge/internal/plaf203"
	"github.com/pion/rtp"
)

const (
	mediaPathPrefix       = "/device/"
	defaultMediaListen    = ":8554"
	mediaClockRate        = 90_000
	mediaTimestampDivisor = time.Second
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
	_ = consumer.Accept()
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
	track       *core.Receiver
	media       *core.Media
	codec       *core.Codec
	startedAt   time.Time
	latestFrame *plaf203.VideoFrame
	mu          sync.Mutex
}

func newMediaPublisher() *mediaPublisher {
	codec := &core.Codec{
		Name:        core.CodecH264,
		ClockRate:   mediaClockRate,
		PayloadType: core.PayloadTypeRAW,
	}
	media := &core.Media{
		Kind:      core.KindVideo,
		Direction: core.DirectionRecvonly,
		Codecs:    []*core.Codec{codec},
	}
	return &mediaPublisher{
		track:     core.NewReceiver(media, codec),
		media:     media,
		codec:     codec,
		startedAt: time.Now(),
	}
}

func (publisher *mediaPublisher) publish(frame *plaf203.VideoFrame) {
	if publisher == nil || frame == nil || len(frame.Data) == 0 {
		return
	}
	publisher.mu.Lock()
	if frame.Keyframe {
		publisher.latestFrame = cloneFrame(frame)
	}
	timestamp := uint32(time.Since(publisher.startedAt) * mediaClockRate / mediaTimestampDivisor)
	publisher.mu.Unlock()
	publisher.track.WriteRTP(&rtp.Packet{
		Header:  rtp.Header{Version: 2, Marker: true, Timestamp: timestamp},
		Payload: frame.Data,
	})
}

func (publisher *mediaPublisher) attach(consumer *rtsp.Conn) error {
	if publisher == nil || consumer == nil {
		return errors.New("camera media consumer is unavailable")
	}
	if err := consumer.AddTrack(publisher.media, publisher.codec, publisher.track); err != nil {
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
