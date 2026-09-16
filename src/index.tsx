import { callable, definePlugin } from "@decky/api";
import { ButtonItem, ConfirmModal, PanelSection, PanelSectionRow, showModal } from "@decky/ui";
import { useEffect, useRef, useState } from "react";
import { FaPlug } from "react-icons/fa";

type Status = {
  devices: { address: string; driver: string | null }[];
  service: Record<string, string>;
  target: Record<string, string>;
  marker: boolean;
  busy: boolean;
  can_restart: boolean;
};

const getStatus = callable<[], Status>("get_status");
const restartGamescope = callable<[], { message: string }>("restart_gamescope");
const errorText = (error: unknown) => error instanceof Error ? error.message : String(error);

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
    <PanelSection title="eGPU · 1002:73ff">
      <PanelSectionRow>
        <div role="status">
          <strong>{status ? (status.devices.length ? "Connected" : "Disconnected") : (error ? "Status unavailable" : "Checking connection…")}</strong>
          {status?.devices.map(device => (
            <div key={device.address}>{device.address} · {device.driver ?? "Driver not ready"}</div>
          ))}
        </div>
      </PanelSectionRow>
      {status && (
        <>
          <PanelSectionRow>
            <div>
              <div>Gamescope: {status.target.ActiveState ?? "unknown"}</div>
              <div>Auto-fix: {status.service.LoadState === "not-found" ? "Not installed" : `${status.service.ActiveState ?? "unknown"} (${status.service.Result ?? "unknown"})`}</div>
              <div>Script marker: {status.marker ? "Present" : "Absent"}</div>
            </div>
          </PanelSectionRow>
          <PanelSectionRow>
            <div style={{ fontSize: 12 }}>
              Connection does not confirm Gamescope is using this GPU.
              The script marker does not confirm a successful fix.
            </div>
          </PanelSectionRow>
        </>
      )}
      {error && <PanelSectionRow><div role="alert">{error}</div></PanelSectionRow>}
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={!status?.can_restart || running || !!error}
          onClick={() => showModal(
            <ConfirmModal
              strTitle="Restart Gamescope?"
              strDescription="This may close your running game and restart Steam. Save your progress first. This manually reapplies the fix without the script's once-per-session guard."
              strOKButtonText="Restart Gamescope"
              strCancelButtonText="Cancel"
              bDestructiveWarning
              onOK={() => { void restart(); }}
            />,
          )}
        >
          {running || status?.busy ? "Restart requested…" : "Restart Gamescope"}
        </ButtonItem>
      </PanelSectionRow>
      {message && <PanelSectionRow><div role="status">{message}</div></PanelSectionRow>}
    </PanelSection>
  );
}

export default definePlugin(() => ({
  name: "eGPU Gamescope",
  content: <Content />,
  icon: <FaPlug />,
}));
