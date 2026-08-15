import { render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CameraPlayer } from "../src/features/camera/CameraPlayer";
import { activateViewer, exchangeWebRtc, heartbeatViewer, releaseViewer, releaseWebRtc } from "../src/api/camera";

vi.mock("../src/api/camera", () => ({
  activateViewer: vi.fn(),
  exchangeWebRtc: vi.fn(),
  heartbeatViewer: vi.fn(),
  releaseViewer: vi.fn(),
  releaseWebRtc: vi.fn(),
}));

class FakePeerConnection {
  public iceGatheringState: RTCIceGatheringState = "complete";
  public localDescription: RTCSessionDescription | null = { type: "offer", sdp: "offer" } as RTCSessionDescription;
  public onconnectionstatechange: (() => void) | null = null;
  public ontrack: ((event: RTCTrackEvent) => void) | null = null;
  public connectionState: RTCPeerConnectionState = "connected";
  public close = vi.fn();
  public addEventListener = vi.fn();
  public removeEventListener = vi.fn();
  public addTransceiver = vi.fn();
  public createOffer = vi.fn().mockResolvedValue({ type: "offer", sdp: "offer" });
  public setLocalDescription = vi.fn().mockResolvedValue(undefined);
  public setRemoteDescription = vi.fn().mockResolvedValue(undefined);
}

class FakeMediaStream {
  public addTrack = vi.fn();
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("CameraPlayer", () => {
  it("registers one viewer and keeps it across ordinary rerenders", async () => {
    vi.stubGlobal("RTCPeerConnection", FakePeerConnection);
    vi.stubGlobal("MediaStream", FakeMediaStream);
    vi.mocked(activateViewer).mockResolvedValue(undefined);
    vi.mocked(exchangeWebRtc).mockResolvedValue({ answer: "answer", sessionId: "a".repeat(32) });
    vi.mocked(heartbeatViewer).mockResolvedValue(undefined);
    vi.mocked(releaseViewer).mockResolvedValue(undefined);
    vi.mocked(releaseWebRtc).mockResolvedValue(undefined);

    const view = render(<CameraPlayer deviceId="device-a" compact />);
    await waitFor(() => expect(activateViewer).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(exchangeWebRtc).toHaveBeenCalledTimes(1));
    view.rerender(<CameraPlayer deviceId="device-a" compact />);
    expect(activateViewer).toHaveBeenCalledTimes(1);
    view.unmount();
    await waitFor(() => expect(releaseViewer).toHaveBeenCalledTimes(1));
    expect(releaseWebRtc).toHaveBeenCalledTimes(1);
  });

  it("closes the prior viewer before connecting a replacement device", async () => {
    vi.stubGlobal("RTCPeerConnection", FakePeerConnection);
    vi.stubGlobal("MediaStream", FakeMediaStream);
    vi.mocked(activateViewer).mockResolvedValue(undefined);
    vi.mocked(exchangeWebRtc).mockResolvedValue({ answer: "answer", sessionId: null });
    vi.mocked(releaseViewer).mockResolvedValue(undefined);

    const view = render(<CameraPlayer deviceId="device-a" />);
    await waitFor(() => expect(activateViewer).toHaveBeenCalledTimes(1));
    view.rerender(<CameraPlayer deviceId="device-b" />);
    await waitFor(() => expect(activateViewer).toHaveBeenCalledTimes(2));
    expect(releaseViewer).toHaveBeenCalledWith("device-a", expect.any(String));
  });
});
