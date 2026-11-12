export default defineAppConfig({
  ui: {
    colors: {
      primary: 'neutral',
      neutral: 'zinc',
    },
    // Раскрытая область выше тостов на отступы; клики ловят только сами тосты.
    toaster: {
      slots: {
        viewport: 'pointer-events-none',
      },
    },
  },
})
