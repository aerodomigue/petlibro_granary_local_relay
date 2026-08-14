package bridge

import (
	"testing"

	"github.com/aerodomigue/petlibro-camera-bridge/internal/plaf203"
)

func TestDeviceIDFromMediaPathOnlyAcceptsOneDeviceScopedRTSPPath(t *testing.T) {
	deviceID, ok := deviceIDFromMediaPath("/device/AF03040302A2B5B2CD60")
	if !ok || deviceID != "AF03040302A2B5B2CD60" {
		t.Fatalf("valid path result device=%q ok=%t", deviceID, ok)
	}
	for _, path := range []string{"/device/", "/streams/device", "/device/../../unsafe"} {
		if _, accepted := deviceIDFromMediaPath(path); accepted {
			t.Fatalf("unsafe media path accepted: %q", path)
		}
	}
}

func TestMediaPublisherAcceptsVerifiedH264FrameWithoutReencoding(t *testing.T) {
	publisher := newMediaPublisher()
	frame := &plaf203.VideoFrame{
		Codec:    "h264",
		Keyframe: true,
		Data:     []byte{0, 0, 0, 1, 0x67, 0x42, 0, 0x29},
	}
	publisher.publish(frame)

	if publisher.track.Packets != 1 {
		t.Fatalf("packets=%d want=1", publisher.track.Packets)
	}
	if publisher.track.Bytes != len(frame.Data) {
		t.Fatalf("bytes=%d want=%d", publisher.track.Bytes, len(frame.Data))
	}
	publisher.mu.Lock()
	defer publisher.mu.Unlock()
	if publisher.latestFrame == nil || string(publisher.latestFrame.Data) != string(frame.Data) {
		t.Fatal("latest H264 keyframe was not retained for a later RTSP consumer")
	}
}
