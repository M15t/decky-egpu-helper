import { callable, definePlugin } from "@decky/api";
import { ButtonItem, ConfirmModal, Field, PanelSection, PanelSectionRow, ToggleField, showModal } from "@decky/ui";
import { useEffect, useRef, useState } from "react";
import { BsGpuCard } from "react-icons/bs";

type Status = {
  devices: { address: string; driver: string | null; name: string | null }[];
  egpu_display: boolean;
  service: Record<string, string>;
  target: Record<string, string>;
  marker: boolean;
  busy: boolean;
  can_restart: boolean;
};

type BoltStatus = {
  available: boolean;
  devices: { id: string; name: string; status: string; link: string | null; power: string | null; generation?: string | null }[];
  error: string | null;
  usb_fallback?: boolean;
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

type InstallState = "current" | "outdated" | "missing" | "foreign";
type AutofixStatus = { state: InstallState; enabled: boolean };
type SleepHookStatus = { state: InstallState | null; enabled: boolean; keep_dock: boolean; busy: boolean; error: string | null; can_change: boolean };
type AutoSwitchStatus = { enabled: boolean; waiting: boolean };

const getStatus = callable<[], Status>("get_status");
const getBoltStatus = callable<[], BoltStatus>("get_bolt_status");
const getBacklightStatus = callable<[], BacklightStatus>("get_backlight_status");
const setBacklightOff = callable<[boolean], BacklightStatus>("set_backlight_off");
const restartGamescope = callable<[], { message: string }>("restart_gamescope");
const getAutofixStatus = callable<[], AutofixStatus>("get_autofix_status");
const installAutofix = callable<[], AutofixStatus>("install_autofix");
const removeAutofix = callable<[], AutofixStatus>("remove_autofix");
const getSleepHookStatus = callable<[], SleepHookStatus>("get_sleep_hook_status");
const setSleepHook = callable<[boolean], SleepHookStatus>("set_sleep_hook");
const setKeepDock = callable<[boolean], SleepHookStatus>("set_keep_dock");
const getAutoSwitch = callable<[], AutoSwitchStatus>("get_auto_switch");
const setAutoSwitch = callable<[boolean], AutoSwitchStatus>("set_auto_switch");
const errorText = (error: unknown) => error instanceof Error ? error.message : String(error);
const joinMeta = (...parts: Array<string | null | undefined | false>) => parts.filter(Boolean).join(" · ");

const boltLabels: Record<string, string> = {
  authorized: "USB4",
  connected: "Connected, not authorized",
  disconnected: "Disconnected",
  connecting: "Connecting",
  authorizing: "Authorizing",
  "auth-error": "Authorization failed",
  unknown: "Unknown",
};

function headline(status: Status | null, error: string) {
  if (!status) return error ? "Status unavailable" : "Checking…";
  const ready = status.devices.find(device => device.driver === "amdgpu");
  if (ready) return `● ${ready.name ?? "eGPU"} ready`;
  if (status.devices.length) return "On PCI · Driver not ready";
  return "GPU not on PCI";
}

function dockLine(bolt: BoltStatus | null) {
  if (!bolt) return "Checking dock…";
  if (!bolt.available) return bolt.error ?? "boltctl unavailable";
  const dock = bolt.devices[0];
  if (!dock) return "No dock listed by boltctl";
  return joinMeta(dock.name, boltLabels[dock.status] ?? dock.status, dock.status === "authorized" && dock.link);
}

function StatusSection({ tick }: { tick: number }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [bolt, setBolt] = useState<BoltStatus | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [running, setRunning] = useState(false);
  const requestPending = useRef(false);
  const boltRequest = useRef(0);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const next = await getStatus();
        if (!disposed) { setStatus(next); setError(""); }
      } catch (failure) {
        if (!disposed) { setStatus(null); setError(errorText(failure)); }
      } finally {
        if (!disposed) timer = setTimeout(refresh, 2000);
      }
    };
    void refresh();
    return () => { disposed = true; clearTimeout(timer); };
  }, []);

  useEffect(() => {
    // Only the newest request may update the dock line.
    const request = ++boltRequest.current;
    setBolt(null);
    getBoltStatus().then(
      next => { if (request === boltRequest.current) setBolt(next); },
      failure => { if (request === boltRequest.current) setBolt({ available: false, devices: [], error: errorText(failure) }); },
    );
  }, [tick]);

  const restart = async () => {
    if (requestPending.current) return;
    requestPending.current = true;
    setRunning(true);
    setMessage("Requesting restart… Steam may disconnect.");
    try {
      setMessage((await restartGamescope()).message);
    } catch (failure) {
      setMessage(`Could not confirm restart: ${errorText(failure)}. Check the session before retrying.`);
    } finally {
      requestPending.current = false;
      setRunning(false);
    }
  };

  const ready = Boolean(status?.devices.some(device => device.driver === "amdgpu"));
  const hint = bolt?.usb_fallback
    ? "Dock is in USB fallback. Press the TB5 button on the dock."
    : bolt?.pci_scan?.error ?? (error && status ? error : null);

  return (
    <PanelSection title="Status">
      <PanelSectionRow>
        <Field label={headline(status, error)} childrenLayout="below" bottomSeparator="none">
          <div>{dockLine(bolt)}</div>
          {ready && <div>{status?.egpu_display ? "Display: eGPU" : "Display: built-in GPU"}</div>}
          {hint && <div>{hint}</div>}
          {!status && error && <div>{error}</div>}
        </Field>
      </PanelSectionRow>
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
          {running || status?.busy ? "Restart requested…" : ready && !status?.egpu_display ? "Restart to use eGPU" : "Restart Gamescope"}
        </ButtonItem>
      </PanelSectionRow>
      {message && (
        <PanelSectionRow>
          <Field childrenLayout="below" bottomSeparator="none">{message}</Field>
        </PanelSectionRow>
      )}
    </PanelSection>
  );
}

function installDescription(state: InstallState | null, foreign: string, normal: string) {
  if (state === "foreign") return foreign;
  if (state === "outdated") return "Update available. Turn on to update.";
  return normal;
}

function AutofixToggle({ tick }: { tick: number }) {
  const [status, setStatus] = useState<AutofixStatus | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  useEffect(() => {
    getAutofixStatus().then(next => { setStatus(next); setError(""); }, failure => setError(errorText(failure)));
  }, [tick]);
  const change = async (on: boolean) => {
    setPending(true);
    setError("");
    try {
      setStatus(await (on ? installAutofix() : removeAutofix()));
    } catch (failure) {
      setError(errorText(failure));
      // A failed systemctl call can follow a successful file write.
      await getAutofixStatus().then(setStatus, () => setStatus(null));
    } finally {
      setPending(false);
    }
  };
  return (
    <PanelSectionRow>
      <ToggleField
        label="Auto-fix at boot"
        checked={status?.state === "current" && status.enabled}
        disabled={pending || !status}
        description={pending ? "Applying…" : error || (!status ? "Checking…" : installDescription(
          status.state,
          "Hand-installed copy found. Turning on replaces it and keeps a .bak.",
          "Restarts Gamescope once at login when the eGPU is on PCI.",
        ))}
        bottomSeparator="none"
        onChange={on => { void change(on); }}
      />
    </PanelSectionRow>
  );
}

function SleepHookToggle({ tick }: { tick: number }) {
  const [status, setStatus] = useState<SleepHookStatus | null>(null);
  const [error, setError] = useState("");
  const [requested, setRequested] = useState<boolean | null>(null);
  const [requestedKeep, setRequestedKeep] = useState<boolean | null>(null);
  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      try {
        const next = await getSleepHookStatus();
        if (disposed) return;
        setStatus(next);
        setError("");
        if (next.busy) timer = setTimeout(refresh, 2000);
        else { setRequested(null); setRequestedKeep(null); }
      } catch (failure) {
        if (!disposed) setError(errorText(failure));
      }
    };
    void refresh();
    return () => { disposed = true; clearTimeout(timer); };
  }, [tick, requested, requestedKeep]);
  const change = async (on: boolean) => {
    setError("");
    try {
      setStatus(await setSleepHook(on));
      setRequested(on);
    } catch (failure) {
      setError(errorText(failure));
    }
  };
  const changeKeep = async (on: boolean) => {
    setError("");
    try {
      setStatus(await setKeepDock(on));
      setRequestedKeep(on);
    } catch (failure) {
      setError(errorText(failure));
    }
  };
  const installed = status?.state === "current" && status.enabled;
  return (
    <>
    <PanelSectionRow>
      <ToggleField
        label="Sleep hook"
        checked={requested ?? installed}
        disabled={!status?.can_change}
        description={status?.busy ? "Approve the administrator prompt…" : error || status?.error || (!status ? "Checking…" : installDescription(
          status.state,
          "Hand-installed hook found. Turning on replaces it and keeps a .bak.",
          "Detaches the eGPU before sleep so the Ally can wake, and keeps the USB4 link awake. Press TB5 on the dock after waking, unless Keep dock linked is on.",
        ))}
        bottomSeparator={installed ? "standard" : "none"}
        onChange={on => { void change(on); }}
      />
    </PanelSectionRow>
    {installed && (
      <PanelSectionRow>
        <ToggleField
          label="Keep dock linked during sleep"
          checked={requestedKeep ?? status.keep_dock}
          disabled={!status.can_change}
          description={status.busy ? "Approve the administrator prompt…" : "Experimental. While a dock is attached, the Ally sleeps lightly so the dock comes back without TB5. The eGPU's fans keep spinning during sleep. One test sleep crashed and rebooted the Ally."}
          bottomSeparator="none"
          onChange={on => { void changeKeep(on); }}
        />
      </PanelSectionRow>
    )}
    </>
  );
}

function AutoSwitchToggle({ tick }: { tick: number }) {
  const [status, setStatus] = useState<AutoSwitchStatus | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    getAutoSwitch().then(next => { setStatus(next); setError(""); }, failure => setError(errorText(failure)));
  }, [tick]);
  const change = async (on: boolean) => {
    setError("");
    try {
      setStatus(await setAutoSwitch(on));
    } catch (failure) {
      setError(errorText(failure));
    }
  };
  return (
    <PanelSectionRow>
      <ToggleField
        label="Auto-switch to eGPU"
        checked={Boolean(status?.enabled)}
        disabled={!status}
        description={error || (status?.waiting
          ? "eGPU is up. Switching after the running game exits."
          : "Restarts Gamescope once when the eGPU comes up, such as after pressing TB5. Waits for a running game to exit.")}
        bottomSeparator="none"
        onChange={on => { void change(on); }}
      />
    </PanelSectionRow>
  );
}

function SetupSection({ tick }: { tick: number }) {
  return (
    <PanelSection title="Setup">
      <AutofixToggle tick={tick} />
      <AutoSwitchToggle tick={tick} />
      <SleepHookToggle tick={tick} />
    </PanelSection>
  );
}

type DpiStatus = {
  available: boolean; installed?: boolean; enabled: boolean; requested: boolean | null;
  busy: boolean; state: string; error: string | null;
  installing: boolean; can_install: boolean; install_error: string | null; install_hint: string | null;
};
const getDpiStatus = callable<[], DpiStatus>("get_dpi_status");
const setDpiEnabled = callable<[boolean], DpiStatus>("set_dpi_enabled");
const installDpiHelper = callable<[], DpiStatus>("install_dpi_helper");

function DpiToggle() {
  const [status, setStatus] = useState<DpiStatus | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const busy = useRef(false);
  const generation = useRef(0);
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const refresh = async () => {
      const revision = generation.current;
      if (!busy.current) {
        try {
          const next = await getDpiStatus();
          if (!disposed && revision === generation.current) { setStatus(next); setError(""); }
        } catch (failure) {
          if (!disposed && revision === generation.current) {
            setStatus(null);
            setError(errorText(failure));
          }
        }
      }
      if (!disposed) timer = setTimeout(refresh, 4000);
    };
    void refresh();
    return () => { disposed = true; mounted.current = false; clearTimeout(timer); };
  }, []);
  const toggle = async (value: boolean) => {
    if (busy.current || !status?.available || status.busy || status.installing) return;
    busy.current = true;
    generation.current += 1;
    setPending(true);
    setError("");
    try {
      const next = await setDpiEnabled(value);
      if (mounted.current) { setStatus(next); setError(next.error ?? ""); }
    } catch (failure) {
      if (mounted.current) { setStatus(null); setError(errorText(failure)); }
    } finally {
      busy.current = false;
      if (mounted.current) setPending(false);
    }
  };
  const install = async () => {
    if (busy.current || !status?.can_install) return;
    busy.current = true;
    generation.current += 1;
    setPending(true);
    setError("");
    try {
      const next = await installDpiHelper();
      if (mounted.current) setStatus(next);
    } catch (failure) {
      if (mounted.current) setError(errorText(failure));
    } finally {
      busy.current = false;
      if (mounted.current) setPending(false);
    }
  };
  return <><PanelSectionRow>
    <ToggleField label="DPI bypass · Zapret2" checked={Boolean(status?.requested)}
      disabled={pending || !status?.available || status.busy || status.installing}
      description={pending ? "Applying…" : error || status?.error || (!status ? "Checking…" : status.enabled ? "Running · Restores ON after reboot" : "Off · Stays OFF after reboot")}
      bottomSeparator="none" onChange={value => { void toggle(value); }} />
  </PanelSectionRow>
    {(status?.installing || status?.installed === false) && <PanelSectionRow>
      <ButtonItem layout="below" disabled={pending || !status.can_install}
        description={status.install_error || status.install_hint || (status.installing
          ? "Approve the Linux administrator prompt. Setup status updates automatically."
          : "One-time setup. Linux may ask for administrator approval.")}
        onClick={() => { void install(); }}>
        {status.installing ? "Installing helper…" : "Install helper"}
      </ButtonItem>
    </PanelSectionRow>}
    {status?.install_error && !status.available && status.installed !== false && !status.installing && <PanelSectionRow>
      <Field label="Helper setup" childrenLayout="below" bottomSeparator="none">{status.install_error}</Field>
    </PanelSectionRow>}
  </>;
}

function ToolsSection({ onRefresh }: { onRefresh: () => void }) {
  const [backlight, setBacklight] = useState<BacklightStatus | null>(null);
  const pending = useRef(false);

  const refresh = async () => {
    try {
      setBacklight(await getBacklightStatus());
    } catch (failure) {
      setBacklight({ available: false, off: false, brightness: null, max: null, saved: null, node: null, error: errorText(failure) });
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
      <DpiToggle />
      <PanelSectionRow>
        <ToggleField
          label="Force off internal backlight"
          checked={Boolean(backlight?.off)}
          disabled={!backlight?.available}
          bottomSeparator="none"
          onChange={(value) => { void toggle(value); }}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={onRefresh}>Refresh</ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}

function Content() {
  const [tick, setTick] = useState(0);
  return (
    <>
      <StatusSection tick={tick} />
      <SetupSection tick={tick} />
      <ToolsSection onRefresh={() => setTick(value => value + 1)} />
    </>
  );
}

export default definePlugin(() => ({
  name: "eGPU Helper",
  content: <Content />,
  icon: <BsGpuCard />,
}));
