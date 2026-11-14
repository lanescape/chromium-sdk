// Linux: Aura on Ozone's headless platform, which needs no display (platform.h).

#include <memory>
#include <optional>
#include <utility>

#include "base/command_line.h"
#include "content/public/browser/web_contents.h"
#include "tests/embedder/platform.h"
#include "ui/aura/window.h"
#include "ui/aura/window_tree_host.h"
#include "ui/gfx/geometry/rect.h"
#include "ui/platform_window/platform_window_init_properties.h"

namespace embedder {
namespace {

constexpr gfx::Rect kBounds(0, 0, 800, 600);
std::unique_ptr<aura::WindowTreeHost> g_host;

}  // namespace

int EarlyMain(int, const char**) {
  return -1;
}

void AppendPlatformSwitches(base::CommandLine& command_line) {
  if (!command_line.HasSwitch("ozone-platform")) {
    command_line.AppendSwitchASCII("ozone-platform", "headless");
  }
}

std::optional<int> PreBrowserMain() {
  return std::nullopt;
}

void ShowContents(content::WebContents* contents) {
  g_host = aura::WindowTreeHost::Create(ui::PlatformWindowInitProperties(kBounds));
  g_host->InitHost();
  g_host->window()->Show();
  g_host->window()->AddChild(contents->GetNativeView());
  contents->GetNativeView()->SetBounds(kBounds);
  contents->GetNativeView()->Show();
  g_host->Show();
}

void HideContents() {
  g_host.reset();
}

}  // namespace embedder
