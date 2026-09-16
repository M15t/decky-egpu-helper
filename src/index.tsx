import { callable, definePlugin } from "@decky/api";
import { ButtonItem, ConfirmModal, PanelSection, PanelSectionRow, ToggleField, showModal } from "@decky/ui";
import { useEffect, useRef, useState } from "react";
import { BsGpuCard } from "react-icons/bs";

type Status = {
  devices: { address: string; driver: string | null; name: string | null }[];
  service: Record<string, string>;
  target: Record<string, string>;
  marker: boolean;
  busy: boolean;
  can_restart: boolean;
};

type BoltStatus = {
  available: boolean;
  devices: { id: string; name: string; status: string; link: string | null; power: string | null }[];
  error: string | null;
  gpu_on_pci?: boolean | null;
  gpu_ready?: boolean | null;
  pci_scan?: { ran: boolean; error: string | null } | null;
};

type BacklightStatus = {
  available: boolean;
  off: boolean;
  brightness: number | null;
  max: number | null;
  saved: number | null;
  node: string | null;
  error: string | null;
};

const getStatus = callable<[], Status>("get_status");
const getBoltStatus = callable<[], BoltStatus>("get_bolt_status");
const getBacklightStatus = callable<[], BacklightStatus>("get_backlight_status");
const setBacklightOff = callable<[boolean], BacklightStatus>("set_backlight_off");
const restartGamescope = callable<[], { message: string }>("restart_gamescope");
const errorText = (error: unknown) => error instanceof Error ? error.message : String(error);

const boltLabels: Record<string, string> = {
  authorized: "Authorized",
  connected: "Connected · Not authorized",
  disconnected: "Disconnected",
  connecting: "Connecting",
  authorizing: "Authorizing",
  "auth-error": "Authorization failed",
  unknown: "Unknown",
};

function BoltSection() {
  const [bolt, setBolt] = useState<BoltStatus | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const refreshNow = useRef<() => void>(() => {});
  useEffect(() => {
    let disposed = false;
    let fetching = false;
    const refresh = async () => {
      if (disposed || fetching) return;
      fetching = true;
      setRefreshing(true);
      try {
        const next = await getBoltStatus();
        if (!disposed) setBolt(next);
      } catch (failure) {
        if (!disposed) setBolt({ available: false, devices: [], error: errorText(failure), gpu_on_pci: null, gpu_ready: null });
      } finally {
        fetching = false;
        if (!disposed) {
          setRefreshing(false);
        }
      }
    };
    refreshNow.current = () => { void refresh(); };
    void refresh();
    return () => {
      disposed = true;
      refreshNow.current = () => {};
    };
  }, []);

  return (
    <PanelSection title="Thunderbolt / USB4">
      <PanelSectionRow>
        <div role="status">
          {!bolt ? "Checking boltctl…" : !bolt.available
            ? `Status unavailable: ${bolt.error ?? "Unknown error"}`
            : !bolt.devices.length ? "No devices listed by boltctl" : null}
        </div>
      </PanelSectionRow>
      {bolt?.devices.map(device => (
        <PanelSectionRow key={device.id}>
          <div>
            <strong>{boltLabels[device.status] ?? device.status}</strong>
            <div>{device.name}</div>
            {device.link && <div>{device.link}</div>}
            {device.power && <div>Power {device.power}</div>}
          </div>
        </PanelSectionRow>
      ))}
      {bolt?.pci_scan?.error && (
        <PanelSectionRow>
          <div role="alert">PCI scan: {bolt.pci_scan.error}</div>
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={refreshing}
          onClick={() => refreshNow.current()}
        >
          {refreshing ? "Refreshing…" : "Refresh"}
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}

function gpuHeadline(status: Status | null, error: string) {
  if (!status) return error ? "Status unavailable" : "Checking connection…";
  if (status.devices.some(device => device.driver === "amdgpu")) return "GPU ready";
  if (status.devices.length) return "On PCI · Driver not ready";
  return "GPU not on PCI";
}

function backlightDescription(backlight: BacklightStatus | null) {
  if (!backlight) return "Checking backlight…";
  if (!backlight.available) return backlight.error || "No internal backlight node";
  if (backlight.off) return undefined;
  if (backlight.brightness != null && backlight.max != null) {
    return `Panel at ${backlight.brightness}/${backlight.max}`;
  }
  return "Internal panel backlight";
}

function ToolsSection() {
  const [backlight, setBacklight] = useState<BacklightStatus | null>(null);
  const pending = useRef(false);

  const refresh = async () => {
    try {
      setBacklight(await getBacklightStatus());
    } catch (failure) {
      setBacklight({
        available: false,
        off: false,
        brightness: null,
        max: null,
        saved: null,
        node: null,
        error: errorText(failure),
      });
    }
  };

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const tick = async () => {
      if (!disposed) {
        await refresh();
        timer = setTimeout(tick, 4000);
      }
    };
    void tick();
    return () => { disposed = true; clearTimeout(timer); };
  }, []);

  const toggle = async (value: boolean) => {
    if (pending.current || !backlight?.available) return;
    pending.current = true;
    setBacklight({ ...backlight, off: value });
    try {
      setBacklight(await setBacklightOff(value));
    } catch (failure) {
      setBacklight({ ...backlight, available: false, error: errorText(failure) });
    } finally {
      pending.current = false;
    }
  };

  return (
    <PanelSection title="Tools">
      <PanelSectionRow>
        <ToggleField
          label="Force off internal backlight"
          description={backlightDescription(backlight)}
          checked={Boolean(backlight?.off)}
          disabled={!backlight?.available}
          onChange={(value) => { void toggle(value); }}
        />
      </PanelSectionRow>
    </PanelSection>
  );
}

function Content() {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [running, setRunning] = useState(false);
  const requestPending = useRef(false);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const next = await getStatus();
        if (!disposed) {
          setStatus(next);
          setError("");
        }
      } catch (failure) {
        if (!disposed) {
          setStatus(null);
          setError(errorText(failure));
        }
      } finally {
        if (!disposed) timer = setTimeout(refresh, 2000);
      }
    };
    void refresh();
    return () => { disposed = true; clearTimeout(timer); };
  }, []);

  const restart = async () => {
    if (requestPending.current) return;
    requestPending.current = true;
    setRunning(true);
    setMessage("Requesting restart… Steam may disconnect.");
    try {
      const result = await restartGamescope();
      setMessage(result.message);
    } catch (failure) {
      setMessage(`Could not confirm restart: ${errorText(failure)}. Check the session before retrying.`);
    } finally {
      requestPending.current = false;
      setRunning(false);
    }
  };

  return (
    <>
      <BoltSection />
      <PanelSection title="eGPU">
        <PanelSectionRow>
          <div role="status">
            <strong>{gpuHeadline(status, error)}</strong>
            {status?.devices.map(device => (
              <div key={device.address}>{device.name || "GPU"}</div>
            ))}
          </div>
        </PanelSectionRow>
        {error && <PanelSectionRow><div role="alert">{error}</div></PanelSectionRow>}
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={running || status?.busy === true}
            onClick={() => showModal(
              <ConfirmModal
                strTitle="Restart Gamescope?"
                strDescription="This may close your game and restart Steam. Save first."
                strOKButtonText="Restart"
                strCancelButtonText="Cancel"
                bDestructiveWarning
                onOK={() => { void restart(); }}
              />,
            )}
          >
            {running || status?.busy ? "Restart requested…" : "Restart"}
          </ButtonItem>
        </PanelSectionRow>
        {message && <PanelSectionRow><div role="status">{message}</div></PanelSectionRow>}
      </PanelSection>
      <ToolsSection />
    </>
  );
}

export default definePlugin(() => ({
  name: "eGPU Helper",
  content: <Content />,
  icon: <BsGpuCard />,
}));
