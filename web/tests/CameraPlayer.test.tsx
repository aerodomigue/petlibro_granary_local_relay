import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CameraPlayer } from "../src/features/camera/CameraPlayer";
import { WebRtcExchangeError, activateViewer, exchangeWebRtc, heartbeatViewer, releaseViewer, releaseWebRtc } from "../src/api/camera";

vi.mock("../src/api/camera", () => ({
  WebRtcExchangeError: class WebRtcExchangeError extends Error {
    public constructor(message: string, public readonly sessionId: string | null) {
      super(message);
    }
  },
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
  reject: (reason: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve: (value: T) => void = () => undefined;
  let reject: (reason: unknown) => void = () => undefined;
  const promise = new Promise<T>((complete, fail) => { resolve = complete; reject = fail; });
  return { promise, resolve, reject };
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
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  FakePeerConnection.instances = [];
});

describe("CameraPlayer", () => {
  it("keeps browser-local audio and fullscreen controls independent from the viewer lifecycle", async () => {
    vi.stubGlobal("RTCPeerConnection", FakePeerConnection);
    vi.stubGlobal("MediaStream", FakeMediaStream);
    configureSuccessfulCameraApi();

    const view = render(<CameraPlayer deviceId="device-a" />);
    const video = view.container.querySelector("video");
    expect(video).not.toBeNull();
    fireEvent.click(view.getByRole("button", { name: "Unmute camera" }));
    await waitFor(() => expect(video?.muted).toBe(false));
    fireEvent.change(view.getByRole("slider", { name: "Camera volume" }), { target: { value: "0.4" } });
    await waitFor(() => expect(video?.volume).toBeCloseTo(0.4));
    expect(view.getByRole("button", { name: "Toggle camera fullscreen" })).toBeVisible();
    expect(activateViewer).toHaveBeenCalledTimes(1);
  });

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

    const view = render(<CameraPlayer deviceId="device-a" />);
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
    expect(releaseWebRtc).toHaveBeenCalledWith("device-a", "late-session");
    expect(FakePeerConnection.instances[0]?.setRemoteDescription).not.toHaveBeenCalled();
  });

  it("releases the WHEP session when the response body fails after its headers", async () => {
    vi.stubGlobal("RTCPeerConnection", FakePeerConnection);
    vi.stubGlobal("MediaStream", FakeMediaStream);
    vi.mocked(activateViewer).mockResolvedValue(undefined);
    vi.mocked(exchangeWebRtc).mockRejectedValue(new WebRtcExchangeError("response body aborted", "late-session"));
    vi.mocked(releaseViewer).mockResolvedValue(undefined);
    vi.mocked(releaseWebRtc).mockResolvedValue(undefined);

    render(<CameraPlayer deviceId="device-a" />);
    await waitFor(() => expect(releaseWebRtc).toHaveBeenCalledWith("device-a", "late-session"));
  });

  it("releases a viewer when an activation response arrives after teardown", async () => {
    vi.stubGlobal("RTCPeerConnection", FakePeerConnection);
    vi.stubGlobal("MediaStream", FakeMediaStream);
    const activation = deferred<void>();
    vi.mocked(activateViewer).mockReturnValue(activation.promise);
    vi.mocked(releaseViewer).mockResolvedValue(undefined);

    const view = render(<CameraPlayer deviceId="device-a" />);
    await flushCameraTasks();
    const viewerId = vi.mocked(activateViewer).mock.calls[0]?.[1];
    view.unmount();
    await act(async () => { activation.resolve(); });

    expect(releaseViewer).toHaveBeenCalledWith("device-a", viewerId);
    expect(releaseViewer).toHaveBeenCalledTimes(2);
  });

  it("does not let a stale heartbeat release a replacement viewer", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("RTCPeerConnection", FakePeerConnection);
    vi.stubGlobal("MediaStream", FakeMediaStream);
    configureSuccessfulCameraApi();
    const heartbeat = deferred<void>();
    vi.mocked(heartbeatViewer).mockReturnValueOnce(heartbeat.promise);

    const view = render(<CameraPlayer deviceId="device-a" />);
    await flushCameraTasks();
    expect(activateViewer).toHaveBeenCalledTimes(1);
    const oldViewerId = vi.mocked(activateViewer).mock.calls[0]?.[1];
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    expect(heartbeatViewer).toHaveBeenCalledTimes(1);

    view.rerender(<CameraPlayer deviceId="device-b" />);
    await flushCameraTasks();
    expect(activateViewer).toHaveBeenCalledTimes(2);
    const newViewerId = vi.mocked(activateViewer).mock.calls[1]?.[1];
    await act(async () => { heartbeat.reject(new Error("old heartbeat failed")); });

    expect(releaseViewer).toHaveBeenCalledWith("device-a", oldViewerId);
    expect(releaseViewer).not.toHaveBeenCalledWith("device-b", newViewerId);
    expect(FakePeerConnection.instances[1]?.close).not.toHaveBeenCalled();
  });

  it("reconnects when a heartbeat cannot finish before its deadline", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("RTCPeerConnection", FakePeerConnection);
    vi.stubGlobal("MediaStream", FakeMediaStream);
    configureSuccessfulCameraApi();
    vi.mocked(heartbeatViewer).mockImplementation((_deviceId, _viewerId, signal) => new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(new DOMException("Heartbeat timed out", "AbortError")), { once: true });
    }));

    render(<CameraPlayer deviceId="device-a" />);
    await flushCameraTasks();
    await act(async () => { await vi.advanceTimersByTimeAsync(5_000); });
    await act(async () => { await vi.advanceTimersByTimeAsync(4_000); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    await flushCameraTasks();

    expect(activateViewer).toHaveBeenCalledTimes(2);
  });

  it("retries after a bounded WHEP timeout and reaches Live after the replacement connects", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("RTCPeerConnection", FakePeerConnection);
    vi.stubGlobal("MediaStream", FakeMediaStream);
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    vi.mocked(activateViewer).mockResolvedValue(undefined);
    vi.mocked(exchangeWebRtc).mockImplementationOnce((_deviceId, _viewerId, _offer, signal) => new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(new DOMException("Timed out", "AbortError")), { once: true });
    }));
    vi.mocked(exchangeWebRtc).mockResolvedValueOnce({ answer: "answer", sessionId: "b".repeat(32) });
    vi.mocked(releaseViewer).mockResolvedValue(undefined);
    vi.mocked(releaseWebRtc).mockResolvedValue(undefined);

    const view = render(<CameraPlayer deviceId="device-a" />);
    await flushCameraTasks();
    expect(exchangeWebRtc).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(7_500); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1_000); });
    await flushCameraTasks();
    expect(activateViewer).toHaveBeenCalledTimes(2);
    expect(exchangeWebRtc).toHaveBeenCalledTimes(2);

    const video = view.container.querySelector("video");
    expect(video).not.toBeNull();
    FakePeerConnection.instances[1]?.ontrack?.({ track: { kind: "video" } } as RTCTrackEvent);
    await act(async () => { video?.dispatchEvent(new Event("loadeddata")); });
    expect(view.getByText("Live", { exact: true })).toBeVisible();
  });

  it("tears down immediately on pagehide and never starts a retry afterwards", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("RTCPeerConnection", FakePeerConnection);
    vi.stubGlobal("MediaStream", FakeMediaStream);
    configureSuccessfulCameraApi();

    render(<CameraPlayer deviceId="device-a" />);
    await flushCameraTasks();
    window.dispatchEvent(new Event("pagehide"));
    expect(releaseViewer).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(activateViewer).toHaveBeenCalledTimes(1);
  });

  it("pauses a hidden tab only after the grace period and waits for an explicit reconnect", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("RTCPeerConnection", FakePeerConnection);
    vi.stubGlobal("MediaStream", FakeMediaStream);
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    configureSuccessfulCameraApi();
    const hidden = vi.spyOn(document, "hidden", "get");
    let isHidden = true;
    hidden.mockImplementation(() => isHidden);

    const view = render(<CameraPlayer deviceId="device-a" />);
    await flushCameraTasks();
    FakePeerConnection.instances[0]?.ontrack?.({ track: { kind: "video" } } as RTCTrackEvent);
    view.container.querySelector("video")?.dispatchEvent(new Event("loadeddata"));
    document.dispatchEvent(new Event("visibilitychange"));
    await act(async () => { await vi.advanceTimersByTimeAsync(14_999); });
    expect(releaseViewer).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(releaseViewer).toHaveBeenCalledTimes(1);

    isHidden = false;
    document.dispatchEvent(new Event("visibilitychange"));
    await flushCameraTasks();
    expect(activateViewer).toHaveBeenCalledTimes(1);
    expect(view.getByText("Camera paused", { exact: true })).toBeVisible();
    await act(async () => { view.getByRole("button", { name: "Reconnect camera" }).click(); });
    await flushCameraTasks();
    expect(activateViewer).toHaveBeenCalledTimes(2);
  });

  it("shows an actionable error after the bounded reconnect budget is exhausted", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("RTCPeerConnection", FakePeerConnection);
    vi.stubGlobal("MediaStream", FakeMediaStream);
    vi.mocked(activateViewer).mockResolvedValue(undefined);
    vi.mocked(exchangeWebRtc).mockRejectedValue(new Error("relay unavailable"));
    vi.mocked(releaseViewer).mockResolvedValue(undefined);
    vi.mocked(releaseWebRtc).mockResolvedValue(undefined);

    const view = render(<CameraPlayer deviceId="device-a" />);
    await flushCameraTasks();
    for (const delay of [1_000, 2_000, 5_000, 10_000]) {
      await act(async () => { await vi.advanceTimersByTimeAsync(delay); });
      await flushCameraTasks();
    }

    expect(view.getByText("Camera unavailable", { exact: true })).toBeVisible();
    expect(activateViewer).toHaveBeenCalledTimes(5);
  });

  it("fails visibly when WHEP succeeds repeatedly but media never arrives", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("RTCPeerConnection", FakePeerConnection);
    vi.stubGlobal("MediaStream", FakeMediaStream);
    configureSuccessfulCameraApi();

    render(<CameraPlayer deviceId="device-a" />);
    await flushCameraTasks();
    for (const retryDelay of [1_000, 2_000, 5_000, 10_000]) {
      await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
      await act(async () => { await vi.advanceTimersByTimeAsync(retryDelay); });
      await flushCameraTasks();
    }
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });

    expect(activateViewer).toHaveBeenCalledTimes(5);
    expect(screen.getByText("Camera unavailable", { exact: true })).toBeVisible();
  });

  it("reconnects after a BFCache pageshow only when the browser restores the page", async () => {
    vi.stubGlobal("RTCPeerConnection", FakePeerConnection);
    vi.stubGlobal("MediaStream", FakeMediaStream);
    configureSuccessfulCameraApi();

    render(<CameraPlayer deviceId="device-a" />);
    await waitFor(() => expect(activateViewer).toHaveBeenCalledTimes(1));
    window.dispatchEvent(new Event("pagehide"));
    const pageShow = new Event("pageshow") as PageTransitionEvent;
    Object.defineProperty(pageShow, "persisted", { value: true });
    window.dispatchEvent(pageShow);
    await waitFor(() => expect(activateViewer).toHaveBeenCalledTimes(2));
  });
});
