// mates: a fresh Qwen pane shows only OpenCode's input box, not the big logo (Wai, 2026-11-12:
// "I just want it to start in chat mode without the big opencode logo"). The home screen's
// logo is a TUI slot; this fills it with an empty box. Anything unexpected (a newer OpenCode
// without this API) leaves the logo as it was rather than breaking the pane.
import { BoxRenderable } from "@opentui/core"

export default {
  id: "mates-no-logo",
  tui: async (api) => {
    api.slots.register({
      order: 100,
      slots: {
        home_logo() {
          try {
            return new BoxRenderable(api.renderer, { height: 0 })
          } catch {
            return undefined
          }
        },
      },
    })
  },
}
