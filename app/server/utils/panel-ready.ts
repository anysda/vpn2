let ready = false

export function markPanelReady(): void {
  ready = true
}

export function isPanelReady(): boolean {
  return ready
}
