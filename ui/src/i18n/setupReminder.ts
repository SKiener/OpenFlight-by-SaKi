/**
 * The kiosk's setup reminder at start: aim the capture camera at the ball, then mark ball and target.
 * `{align}` is the footer's Align label (range.align), so the step names the button the user sees.
 */
export const setupReminderEn = {
  'setup.eyebrow': 'Setup',
  'setup.title': 'Aim the camera at the ball',
  'setup.stepBall': 'Put a ball on the hitting spot.',
  'setup.stepCamera': 'Adjust the camera until the ball sits on the crosshair.',
  'setup.stepMarks': 'Tap ball and target under “{align}”.',
  'setup.ballMark': 'Ball mark: {state}',
  'setup.targetMark': 'Target: {state}',
  'setup.markSet': 'set',
  'setup.markMissing': 'missing',
  'setup.done': 'Done',
  'setup.loading': 'Loading camera picture …',
  'setup.noPicture': 'No camera picture',
};

export const setupReminderDe: Record<keyof typeof setupReminderEn, string> = {
  'setup.eyebrow': 'Setup',
  'setup.title': 'Kamera auf den Ball einstellen',
  'setup.stepBall': 'Ball auf die Abschlagposition legen.',
  'setup.stepCamera': 'Kamera einstellen, bis der Ball im Fadenkreuz liegt.',
  'setup.stepMarks': 'Ball und Ziel unter „{align}“ antippen.',
  'setup.ballMark': 'Ballmarke: {state}',
  'setup.targetMark': 'Ziel: {state}',
  'setup.markSet': 'gesetzt',
  'setup.markMissing': 'fehlt',
  'setup.done': 'Fertig',
  'setup.loading': 'Kamerabild lädt …',
  'setup.noPicture': 'Kein Kamerabild',
};

export const setupReminderEs: Record<keyof typeof setupReminderEn, string> = {
  'setup.eyebrow': 'Preparación',
  'setup.title': 'Apunta la cámara a la bola',
  'setup.stepBall': 'Coloca una bola en el punto de golpeo.',
  'setup.stepCamera': 'Ajusta la cámara hasta que la bola quede en la cruz.',
  'setup.stepMarks': 'Toca la bola y el objetivo en «{align}».',
  'setup.ballMark': 'Marca de bola: {state}',
  'setup.targetMark': 'Objetivo: {state}',
  'setup.markSet': 'fijada',
  'setup.markMissing': 'falta',
  'setup.done': 'Listo',
  'setup.loading': 'Cargando imagen de la cámara …',
  'setup.noPicture': 'Sin imagen de la cámara',
};
