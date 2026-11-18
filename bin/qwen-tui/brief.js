// mates: OpenCode's side panel shows the task this pane was given, at the top (Wai, 2026-11-18:
// "show the overall plan and/or the instigating prompt in that Qwen panel"). mates writes the
// lead's brief for this pane to a folder bin/qwen mounts read-only at /opt/mates-brief, so Qwen
// cannot change what the panel says it was asked; Qwen's own plan shows lower in the panel, in
// OpenCode's todo list. The text is re-read every few seconds. Anything unexpected (a newer
// OpenCode without this API) leaves the panel as it was rather than breaking the pane.
import { BoxRenderable, TextRenderable } from "@opentui/core"
import { readFileSync } from "node:fs"

const FILE = "/opt/mates-brief/brief.md"
const MAX_LINES = 18

function brief() {
  try {
    const lines = readFileSync(FILE, "utf8").trim().split("\n")
    if (lines.length === 1 && !lines[0]) return ""
    return lines.slice(0, MAX_LINES).join("\n") + (lines.length > MAX_LINES ? "\n…" : "")
  } catch {
    return ""
  }
}

export default {
  id: "mates-brief",
  tui: async (api) => {
    api.slots.register({
      order: 50,
      slots: {
        sidebar_content() {
          try {
            const theme = api.theme?.current ?? {}
            const box = new BoxRenderable(api.renderer, { flexDirection: "column", flexShrink: 0 })
            const head = new TextRenderable(api.renderer, { content: "Task (from the lead)", fg: theme.text })
            const body = new TextRenderable(api.renderer, { content: brief(), fg: theme.textMuted, wrapMode: "word" })
            box.add(head)
            box.add(body)
            const timer = setInterval(() => {
              try {
                const text = brief()
                if (body.content !== text) body.content = text
              } catch {}
            }, 3000)
            timer.unref?.()
            return box
          } catch {
            return undefined
          }
        },
      },
    })
  },
}
