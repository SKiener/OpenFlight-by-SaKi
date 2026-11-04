export const demoEn = {
  'demo.title': 'Demo',
  'demo.mode': 'Demo mode',
  'demo.explain': 'Shows every feature with automatic demo shots. Real shots are ignored.',
  'demo.band': 'DEMO – real shots are ignored',
  'demo.end': 'End',
  'demo.ballModeOnly': 'Ball mode only',
} as const;

export const demoDe: Record<keyof typeof demoEn, string> = {
  'demo.title': 'Demo',
  'demo.mode': 'Demo-Modus',
  'demo.explain': 'Zeigt alle Funktionen mit automatischen Demo-Schlägen. Echte Schläge werden ignoriert.',
  'demo.band': 'DEMO – echte Schläge werden ignoriert',
  'demo.end': 'Beenden',
  'demo.ballModeOnly': 'Nur im Ballmodus',
};
