import { createContext, useCallback, useContext, useMemo, useState, type JSX, type ReactNode } from "react";

const ADVANCED_MODE_STORAGE_KEY = "petlibro-advanced-mode";
const DEVICE_NAMES_STORAGE_KEY = "petlibro-device-names";

interface Preferences {
  advancedMode: boolean;
  deviceNames: Readonly<Record<string, string>>;
  setAdvancedMode: (enabled: boolean) => void;
  setDeviceName: (deviceId: string, name: string) => void;
}

const DEFAULT_PREFERENCES: Preferences = {
  advancedMode: false,
  deviceNames: {},
  setAdvancedMode: () => undefined,
  setDeviceName: () => undefined,
};

const PreferencesContext = createContext<Preferences>(DEFAULT_PREFERENCES);

function readAdvancedMode(): boolean {
  try {
    return window.localStorage.getItem(ADVANCED_MODE_STORAGE_KEY) === "true";
  } catch {
    return false;
  }
}

function readDeviceNames(): Readonly<Record<string, string>> {
  try {
    const stored = JSON.parse(window.localStorage.getItem(DEVICE_NAMES_STORAGE_KEY) ?? "{}") as unknown;
    if (stored === null || typeof stored !== "object" || Array.isArray(stored)) return {};
    return Object.fromEntries(Object.entries(stored).filter((entry): entry is [string, string] => typeof entry[1] === "string"));
  } catch {
    return {};
  }
}

/** Own browser-local UI preferences so routes never read localStorage independently. */
export function PreferencesProvider({ children }: { children: ReactNode }): JSX.Element {
  const [advancedMode, setAdvancedModeState] = useState(readAdvancedMode);
  const [deviceNames, setDeviceNames] = useState(readDeviceNames);
  const setAdvancedMode = useCallback((enabled: boolean) => {
    try { window.localStorage.setItem(ADVANCED_MODE_STORAGE_KEY, String(enabled)); } catch { /* Browser privacy mode can disable storage. */ }
    setAdvancedModeState(enabled);
  }, []);
  const setDeviceName = useCallback((deviceId: string, name: string) => {
    setDeviceNames((current) => {
      const next = { ...current };
      if (name) next[deviceId] = name; else delete next[deviceId];
      try { window.localStorage.setItem(DEVICE_NAMES_STORAGE_KEY, JSON.stringify(next)); } catch { /* Keep the in-memory preference. */ }
      return next;
    });
  }, []);
  const value = useMemo(() => ({ advancedMode, deviceNames, setAdvancedMode, setDeviceName }), [advancedMode, deviceNames, setAdvancedMode, setDeviceName]);
  return <PreferencesContext.Provider value={value}>{children}</PreferencesContext.Provider>;
}

export function usePreferences(): Preferences {
  return useContext(PreferencesContext);
}
