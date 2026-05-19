export default defineAppConfig({
  ui: {
    colors: {
      primary: 'emerald',
      neutral: 'zinc',
    },
    card: {
      slots: {
        root: 'bg-zinc-900 border border-zinc-800/60 ring-0 shadow-xs',
        header: 'px-4 py-3 border-b border-zinc-800/60',
        body: 'px-4 py-3',
      },
    },
  },
})
