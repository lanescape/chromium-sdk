// A minimal Content API embedder, built outside Chromium's tree against a packaged SDK (tests/embedder/build.py),
// that checks the SDK works end to end: it starts Content, loads a page whose script computes a value, reads the
// value back through JavaScript in an isolated world, and exits 0 if it's right (specs/0003-ci-cd.md, Tests).
//
// It implements Content's delegate interfaces in its own binary, so the library calls back across its boundary,
// and it runs Chromium's renderer and GPU processes from its own executable, sandboxed as Chromium sets up.

#include <stdio.h>

#include <memory>
#include <optional>
#include <string>
#include <utility>

#include "base/base_paths.h"
#include "base/command_line.h"
#include "base/files/file_path.h"
#include "base/files/scoped_temp_dir.h"
#include "base/functional/bind.h"
#include "base/functional/callback.h"
#include "base/location.h"
#include "base/path_service.h"
#include "base/run_loop.h"
#include "base/task/single_thread_task_runner.h"
#include "base/time/time.h"
#include "base/values.h"
#include "build/build_config.h"
#include "content/public/app/content_main.h"
#include "content/public/app/content_main_delegate.h"
#include "content/public/browser/browser_context.h"
#include "content/public/browser/browser_main_parts.h"
#include "content/public/browser/content_browser_client.h"
#include "content/public/browser/navigation_controller.h"
#include "content/public/browser/render_frame_host.h"
#include "content/public/browser/web_contents.h"
#include "content/public/browser/web_contents_observer.h"
#include "content/public/common/isolated_world_ids.h"
#include "tests/embedder/platform.h"
#include "ui/base/resource/resource_bundle.h"
#include "ui/base/resource/resource_scale_factor.h"
#include "url/gurl.h"

namespace embedder {
namespace {

// The page computes the value; the embedder only reads it.
constexpr char kPage[] =
    "data:text/html,<title>pending</title><script>document.title = String(6 * 7)</script>";
constexpr char16_t kRead[] = u"document.title";
constexpr char kExpected[] = "42";
constexpr base::TimeDelta kTimeout = base::Seconds(90);

int g_result = 1;

class BrowserContext : public content::BrowserContext {
 public:
  BrowserContext() { CHECK(directory_.CreateUniqueTempDir()); }
  ~BrowserContext() override {
    NotifyWillBeDestroyed();
    ShutdownStoragePartitions();
  }

  std::unique_ptr<content::ZoomLevelDelegate> CreateZoomLevelDelegate(const base::FilePath&) override {
    return nullptr;
  }
  base::FilePath GetPath() const override { return directory_.GetPath(); }
  bool IsOffTheRecord() override { return true; }
  content::DownloadManagerDelegate* GetDownloadManagerDelegate() override { return nullptr; }
  content::BrowserPluginGuestManager* GetGuestManager() override { return nullptr; }
  storage::SpecialStoragePolicy* GetSpecialStoragePolicy() override { return nullptr; }
  content::PlatformNotificationService* GetPlatformNotificationService() override { return nullptr; }
  content::PushMessagingService* GetPushMessagingService() override { return nullptr; }
  content::StorageNotificationService* GetStorageNotificationService() override { return nullptr; }
  content::SSLHostStateDelegate* GetSSLHostStateDelegate() override { return nullptr; }
  content::PermissionControllerDelegate* GetPermissionControllerDelegate() override { return nullptr; }
  content::ReduceAcceptLanguageControllerDelegate* GetReduceAcceptLanguageControllerDelegate() override {
    return nullptr;
  }
  content::ClientHintsControllerDelegate* GetClientHintsControllerDelegate() override { return nullptr; }
  content::BackgroundFetchDelegate* GetBackgroundFetchDelegate() override { return nullptr; }
  content::BackgroundSyncController* GetBackgroundSyncController() override { return nullptr; }
  content::BrowsingDataRemoverDelegate* GetBrowsingDataRemoverDelegate() override { return nullptr; }

 private:
  base::ScopedTempDir directory_;
};

// Waits for the page to load, then reads its result through an isolated world, as an embedder's own scripts do.
class Check : public content::WebContentsObserver {
 public:
  Check(content::WebContents* contents, base::OnceClosure done)
      : content::WebContentsObserver(contents), done_(std::move(done)) {}

  void DidFinishLoad(content::RenderFrameHost* frame, const GURL& url) override {
    if (frame != web_contents()->GetPrimaryMainFrame() || asked_) {
      return;
    }
    asked_ = true;
    fprintf(stderr, "embedder: loaded %s\n", url.possibly_invalid_spec().substr(0, 40).c_str());
    frame->ExecuteJavaScriptInIsolatedWorld(kRead, base::BindOnce(&Check::OnResult, base::Unretained(this)),
                                            content::ISOLATED_WORLD_ID_CONTENT_END);
  }

  void PrimaryMainFrameRenderProcessGone(base::TerminationStatus status) override {
    fprintf(stderr, "embedder: the renderer is gone (status %d)\n", static_cast<int>(status));
    Finish(1);
  }

 private:
  void OnResult(base::Value value) {
    if (value.is_string() && value.GetString() == kExpected) {
      fprintf(stderr, "embedder: the page computed %s\n", kExpected);
      Finish(0);
      return;
    }
    fprintf(stderr, "embedder: expected \"%s\", got %s\n", kExpected, value.DebugString().c_str());
    Finish(1);
  }

  void Finish(int result) {
    g_result = result;
    if (done_) {
      std::move(done_).Run();
    }
  }

  base::OnceClosure done_;
  bool asked_ = false;
};

class MainParts : public content::BrowserMainParts {
 public:
  int PreMainMessageLoopRun() override {
    context_ = std::make_unique<BrowserContext>();
    contents_ = content::WebContents::Create(content::WebContents::CreateParams(context_.get()));
    ShowContents(contents_.get());
    return 0;
  }

  void WillRunMainMessageLoop(std::unique_ptr<base::RunLoop>& run_loop) override {
    check_ = std::make_unique<Check>(contents_.get(), run_loop->QuitClosure());
    contents_->GetController().LoadURLWithParams(content::NavigationController::LoadURLParams(GURL(kPage)));
    base::SingleThreadTaskRunner::GetCurrentDefault()->PostDelayedTask(
        FROM_HERE,
        base::BindOnce(
            [](base::OnceClosure quit) {
              fprintf(stderr, "embedder: no result after %d s\n", static_cast<int>(kTimeout.InSeconds()));
              g_result = 1;
              std::move(quit).Run();
            },
            run_loop->QuitClosure()),
        kTimeout);
  }

  void PostMainMessageLoopRun() override {
    check_.reset();
    contents_.reset();
    HideContents();
    context_.reset();
  }

 private:
  std::unique_ptr<BrowserContext> context_;
  std::unique_ptr<content::WebContents> contents_;
  std::unique_ptr<Check> check_;
};

class BrowserClient : public content::ContentBrowserClient {
 public:
  std::unique_ptr<content::BrowserMainParts> CreateBrowserMainParts(bool) override {
    return std::make_unique<MainParts>();
  }
};

class MainDelegate : public content::ContentMainDelegate {
 public:
  std::optional<int> BasicStartupComplete() override {
    AppendPlatformSwitches(*base::CommandLine::ForCurrentProcess());
    return std::nullopt;
  }

  // Every process loads the SDK's resources and English strings from beside the executable.
  void PreSandboxStartup() override {
    base::FilePath assets;
    CHECK(base::PathService::Get(base::DIR_ASSETS, &assets));
    ui::ResourceBundle::InitSharedInstanceWithPakPath(assets.AppendASCII("locales").AppendASCII("en-US.pak"));
    ui::ResourceBundle::GetSharedInstance().AddDataPackFromPath(assets.AppendASCII("resources.pak"),
                                                                ui::k100Percent);
  }

  std::optional<int> PreBrowserMain() override {
    if (std::optional<int> stop = content::ContentMainDelegate::PreBrowserMain()) {
      return stop;
    }
    return embedder::PreBrowserMain();
  }

  content::ContentBrowserClient* CreateContentBrowserClient() override {
    browser_client_ = std::make_unique<BrowserClient>();
    return browser_client_.get();
  }

 private:
  std::unique_ptr<BrowserClient> browser_client_;
};

}  // namespace
}  // namespace embedder

int main(int argc, const char** argv) {
  if (const int stop = embedder::EarlyMain(argc, argv); stop >= 0) {
    return stop;
  }
  embedder::MainDelegate delegate;
  content::ContentMainParams params(&delegate);
  params.argc = argc;
  params.argv = argv;
  const int code = content::ContentMain(std::move(params));
  // Only the browser process has a result; a child's code is Content's own.
  bool child = false;
  for (int i = 1; i < argc; ++i) {
    child = child || std::string(argv[i]).rfind("--type=", 0) == 0;
  }
  return child || code != 0 ? code : embedder::g_result;
}
