export function watchTargetList(screen: EventTarget, inside: (event: Event) => boolean, close: () => void): () => void {
  const onPointerDown = (event: Event): void => {
    if (!inside(event)) close();
  };
  const onKeyDown = (event: Event): void => {
    if ((event as KeyboardEvent).key === 'Escape') close();
  };
  screen.addEventListener('pointerdown', onPointerDown);
  screen.addEventListener('keydown', onKeyDown);
  return () => {
    screen.removeEventListener('pointerdown', onPointerDown);
    screen.removeEventListener('keydown', onKeyDown);
  };
}
