export default defineAppConfig({
  ui: {
    colors: {
      primary: 'violet',
      neutral: 'zinc',
    },
    card: {
      slots: {
        root: 'bg-(--ui-bg-elevated) border border-(--ui-border) ring-0 shadow-xs',
        header: 'px-4 py-3 border-b border-(--ui-border-muted)',
        body: 'px-4 py-3',
      },
    },
  },
})
