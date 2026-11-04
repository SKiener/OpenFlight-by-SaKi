import { createRoot } from 'react-dom/client';
import { useState } from 'react';
import { DebugPanel } from '../../../src/components/DebugPanel';
import { useLocaleStore } from '../../../src/stores/useLocaleStore';
import type { OpsRecoveryResult } from '../../../src/types/socket';
import '../../../src/index.css';
import '../../../src/components/panel/panel.css';

useLocaleStore.getState().setLocale('de');
let calls = 0;
let complete: (result: OpsRecoveryResult) => void;
Object.assign(window, { recoveryTest: { calls: () => calls, finish: (result: OpsRecoveryResult) => complete(result) } });

export function Fixture() {
  const [config, setConfig] = useState({ min_speed: 10, max_speed: 220, min_magnitude: 0, transmit_power: 3 });
  const [error, setError] = useState<'busy' | null>(null);
  return <div className="panel-app">
    <main className="panel-app__main"><div className="panel">
      <header className="panel__header">Diagnose</header>
      <div className="panel__body panel-app__debug">
        <DebugPanel enabled={false} readings={[]} shotLogs={[]} mockMode={false} connected demoActive={false}
          radarConfig={config} radarConfigError={error}
          onToggle={() => {}} onUpdateConfig={() => { setConfig({ ...config }); setError('busy'); }} triggerDiagnostics={[]}
          triggerStatus={{ mode: 'rolling-buffer', trigger_type: 'sound', radar_connected: true,
            radar_port: '/dev/ttyAMA0', triggers_total: 4, triggers_accepted: 3, triggers_rejected: 1 }}
          onReinitializeRadar={() => { calls += 1; return new Promise((resolve) => { complete = resolve; }); }} />
      </div>
    </div></main>
  </div>;
}
createRoot(document.getElementById('root')!).render(<Fixture />);
