import { act, render, waitFor } from "@testing-library/react";
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
  public static instances: FakePeerConnection[] = [];
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

  public constructor() {
    FakePeerConnection.instances.push(this);
  }
}

class FakeMediaStream {
  public addTrack = vi.fn();
}

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve: (value: T) => void = () => undefined;
  const promise = new Promise<T>((complete) => { resolve = complete; });
  return { promise, resolve };
}

function configureSuccessfulCameraApi(): void {
  vi.mocked(activateViewer).mockResolvedValue(undefined);
  vi.mocked(exchangeWebRtc).mockResolvedValue({ answer: "answer", sessionId: "a".repeat(32) });
  vi.mocked(heartbeatViewer).mockResolvedValue(undefined);
  vi.mocked(releaseViewer).mockResolvedValue(undefined);
  vi.mocked(releaseWebRtc).mockResolvedValue(undefined);
}

async function flushCameraTasks(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  FakePeerConnection.instances = [];
});

describe("CameraPlayer", () => {
  it("registers one viewer and keeps it across ordinary rerenders", async () => {
    vi.stubGlobal("RTCPeerConnection", FakePeerConnection);
    vi.stubGlobal("MediaStream", FakeMediaStream);
    configureSuccessfulCameraApi();

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
    configureSuccessfulCameraApi();
    vi.mocked(exchangeWebRtc).mockResolvedValue({ answer: "answer", sessionId: null });

    const view = render(<CameraPlayer deviceId="device-a" />);
    await waitFor(() => expect(activateViewer).toHaveBeenCalledTimes(1));
    view.rerender(<CameraPlayer deviceId="device-b" />);
    await waitFor(() => expect(activateViewer).toHaveBeenCalledTimes(2));
    expect(releaseViewer).toHaveBeenCalledWith("device-a", expect.any(String));
  });

  it("creates a viewer when the browser does not expose crypto.randomUUID", async () => {
    vi.stubGlobal("crypto", {});
    vi.stubGlobal("RTCPeerConnection", FakePeerConnection);
    vi.stubGlobal("MediaStream", FakeMediaStream);
    configureSuccessfulCameraApi();

    render(<CameraPlayer deviceId="device-a" />);
    await waitFor(() => expect(activateViewer).toHaveBeenCalledTimes(1));
    expect(vi.mocked(activateViewer).mock.calls[0]?.[1]).toMatch(/^[0-9a-f]{32}$/);
  });

  it("sends heartbeats after activation and stops them on teardown", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("RTCPeerConnection", FakePeerConnection);
    vi.stubGlobal("MediaStream", FakeMediaStream);
    configureSuccessfulCameraApi();

    const view = render(<CameraPlayer deviceId="device-a" />);
    await flushCameraTasks();
    expect(exchangeWebRtc).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(heartbeatViewer).toHaveBeenCalledTimes(1);

    view.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(heartbeatViewer).toHaveBeenCalledTimes(1);
  });

  it("does not run a scheduled retry after unmount", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("RTCPeerConnection", FakePeerConnection);
    vi.stubGlobal("MediaStream", FakeMediaStream);
    vi.mocked(activateViewer).mockResolvedValue(undefined);
    vi.mocked(exchangeWebRtc).mockRejectedValue(new Error("WHEP unavailable"));
    vi.mocked(releaseViewer).mockResolvedValue(undefined);
    vi.mocked(releaseWebRtc).mockResolvedValue(undefined);

    const view = render(<CameraPlayer deviceId="device-a" />);
    await flushCameraTasks();
    expect(exchangeWebRtc).toHaveBeenCalledTimes(1);
    await flushCameraTasks();
    expect(releaseViewer).toHaveBeenCalledTimes(1);
    view.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(activateViewer).toHaveBeenCalledTimes(1);
  });

  it("aborts and ignores an in-flight WHEP exchange during teardown", async () => {
    vi.stubGlobal("RTCPeerConnection", FakePeerConnection);
    vi.stubGlobal("MediaStream", FakeMediaStream);
    const exchange = deferred<{ answer: string; sessionId: string | null }>();
    vi.mocked(activateViewer).mockResolvedValue(undefined);
    vi.mocked(exchangeWebRtc).mockImplementation((_deviceId, _viewerId, _offer, signal) => {
      expect(signal.aborted).toBe(false);
      return exchange.promise;
    });
    vi.mocked(releaseViewer).mockResolvedValue(undefined);

    const view = render(<CameraPlayer deviceId="device-a" />);
    await flushCameraTasks();
    expect(exchangeWebRtc).toHaveBeenCalledTimes(1);
    const signal = vi.mocked(exchangeWebRtc).mock.calls[0]?.[3];
    view.unmount();
    expect(signal?.aborted).toBe(true);

    await act(async () => { exchange.resolve({ answer: "late-answer", sessionId: "late-session" }); });
    expect(releaseWebRtc).not.toHaveBeenCalled();
    expect(FakePeerConnection.instances[0]?.setRemoteDescription).not.toHaveBeenCalled();
  });
});
