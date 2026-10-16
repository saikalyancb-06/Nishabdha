import { memo, useEffect, useRef, useState, lazy, Suspense } from "react";
import {
  ArrowUpRight,
  AudioLines,
  BookOpen,
  ChevronRight,
  CircleHelp,
  Fingerprint,
  KeyRound,
  Layers3,
  LockKeyhole,
  Menu,
  MessageSquareText,
  ShieldCheck,
  Smartphone,
  Sparkles,
} from "lucide-react";
import { Button } from "./components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
} from "./components/ui/dialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogTitle,
} from "./components/ui/alert-dialog";
import workspace from "./workspace.html?raw";
const PolicyAssistant = lazy(() => import("./PolicyAssistant"));
type Tab = "call" | "phone" | "desk" | "knowledge" | "demo";
type Transfer = {
  amount_paise: number;
  payee_name: string;
  destination_last4: string;
  source_last4: string;
  currency: string;
};
declare global {
  interface Window {
    confirmTransaction?: (transaction: Transfer) => Promise<boolean>;
  }
}
const pages = {
  call: {
    label: "Voice workspace",
    icon: AudioLines,
    eyebrow: "VOICE BANKING, WITH ASSURANCE",
    title: "Every voice. Verified.",
    description:
      "A conversation that feels natural. Controls that keep you in charge.",
  },
  phone: {
    label: "Customer app",
    icon: Smartphone,
    eyebrow: "YOUR ACCOUNT. YOUR PERMISSION.",
    title: "You make the final call.",
    description:
      "Review the exact action, verify your bank, and approve with a passkey.",
  },
  desk: {
    label: "Fraud desk",
    icon: ShieldCheck,
    eyebrow: "EVIDENCE BEFORE ACTION",
    title: "See the signal. Understand the decision.",
    description:
      "Follow live sessions, inspect voice evidence, and resolve flagged activity.",
  },
  knowledge: {
    label: "Knowledge library",
    icon: BookOpen,
    eyebrow: "REVIEWED SOURCES, CLEAR ANSWERS",
    title: "Good answers start with evidence.",
    description:
      "Explore approved policies and review new sources before they reach a caller.",
  },
  demo: {
    label: "Judge Demo",
    icon: Sparkles,
    eyebrow: "LIVE EVALUATION & VERIFICATION",
    title: "Unclone Agent Guard Demo",
    description:
      "Live demonstration mode for hackathon judging: 4 core attack scenarios, live microphone input, 9-stage verification timeline, and deterministic action gateway enforcement.",
  },
};
const Workspace = memo(function Workspace() {
  const host = useRef<HTMLDivElement>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    const load = (src: string) =>
      new Promise<void>((resolve, reject) => {
        const script = document.createElement("script");
        script.src = src;
        script.onload = () => resolve();
        script.onerror = () => reject(new Error("Unable to load " + src));
        document.body.append(script);
      });
    void (async () => {
      try {
        await load("/static/workspace.js");
        await load("/static/security.js");
        if (!cancelled) document.dispatchEvent(new Event("nishabdha:ready"));
      } catch {
        if (!cancelled)
          setError("The workspace could not load. Refresh the page to retry.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);
  return (
    <>
      {error && (
        <div className="inline-error" role="alert">
          {error}
        </div>
      )}
      <div ref={host} dangerouslySetInnerHTML={{ __html: workspace }} />
    </>
  );
});
export default function App() {
  const [tab, setTab] = useState<Tab>("demo");
  const [mobile, setMobile] = useState(false);
  const [compact, setCompact] = useState(
    () => matchMedia("(max-width:760px)").matches,
  );
  const sidebar = useRef<HTMLElement>(null);
  const [assistant, setAssistant] = useState(false);
  const [ready, setReady] = useState(false);
  const [health, setHealth] = useState<"loading" | "online" | "offline">(
    "loading",
  );
  const [provider, setProvider] = useState("Checking providers");
  const [transaction, setTransaction] = useState<Transfer | null>(null);
  const approval = useRef<((approved: boolean) => void) | null>(null);
  useEffect(() => {
    const media = matchMedia("(max-width:760px)");
    const update = () => {
      setCompact(media.matches);
      if (!media.matches) setMobile(false);
    };
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    if (!compact || !mobile) return;
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const items = () =>
      Array.from(
        sidebar.current?.querySelectorAll<HTMLElement>(
          "a[href],button:not([disabled]),summary,input:not([disabled]),select:not([disabled])",
        ) || [],
      ).filter((el) => el.offsetParent !== null);
    items()[0]?.focus();
    const keyboard = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setMobile(false);
      }
      if (event.key === "Tab") {
        const focusable = items();
        const first = focusable[0];
        const last = focusable.at(-1);
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    };
    document.addEventListener("keydown", keyboard);
    return () => {
      document.removeEventListener("keydown", keyboard);
      document.body.style.overflow = overflow;
      previous?.focus();
    };
  }, [compact, mobile]);
  useEffect(() => {
    const change = (e: Event) => {
      const name = (e as CustomEvent).detail as Tab;
      if (name in pages) setTab(name);
    };
    const loaded = () => setReady(true);
    document.addEventListener("nishabdha:tab", change);
    document.addEventListener("nishabdha:ready", loaded);
    window.confirmTransaction = (t) =>
      new Promise((resolve) => {
        approval.current = resolve;
        setTransaction(t);
      });
    let current = true;
    const poll = () =>
      fetch("/api/health")
        .then((r) => {
          if (!r.ok) throw Error();
          return r.json();
        })
        .then((h) => {
          if (!current) return;
          setHealth("online");
          const chain = h.voice?.chain || [];
          setProvider(
            chain.length ? chain.join(" + ") : "Voice provider unavailable",
          );
        })
        .catch(() => {
          if (current) {
            setHealth("offline");
            setProvider("Backend unavailable");
          }
        });
    void poll();
    const timer = setInterval(poll, 30000);
    return () => {
      current = false;
      clearInterval(timer);
      document.removeEventListener("nishabdha:tab", change);
      document.removeEventListener("nishabdha:ready", loaded);
      approval.current?.(false);
      delete window.confirmTransaction;
    };
  }, []);
  const navigate = (next: Tab) => {
    setTab(next);
    setMobile(false);
    document.dispatchEvent(
      new CustomEvent("nishabdha:navigate", { detail: next }),
    );
  };
  const resolve = (approved: boolean) => {
    approval.current?.(approved);
    approval.current = null;
    setTransaction(null);
  };
  const current = pages[tab];
  return (
    <div className="app-shell">
      <a className="skip-link" href="#workspace">
        Skip to workspace
      </a>
      {mobile && (
        <button
          className="mobile-scrim"
          aria-label="Close navigation"
          tabIndex={-1}
          onClick={() => setMobile(false)}
        />
      )}
      <aside
        ref={sidebar}
        className={"sidebar " + (mobile ? "sidebar-open" : "")}
        inert={compact && !mobile}
        aria-hidden={compact && !mobile}
        role={compact && mobile ? "dialog" : undefined}
        aria-modal={compact && mobile ? true : undefined}
        aria-label="Workspace navigation"
      >
        <a className="brand-lockup" href="/" aria-label="Nishabdha home">
          <img src="/static/mark.svg" alt="" />
          <div>
            Nishabdha<span>THE VOICE TRUST LAYER</span>
          </div>
        </a>
        <div className="nav-section-label">WORKSPACE</div>
        <nav className="tabs" aria-label="Main navigation">
          {(Object.keys(pages) as Tab[]).map((key, i) => {
            const Icon = pages[key].icon;
            return (
              <button
                key={key}
                data-tab={key}
                aria-current={tab === key ? "page" : undefined}
                disabled={!ready}
                onClick={() => navigate(key)}
              >
                <Icon size={19} />
                <span>{pages[key].label}</span>
                <span className="nav-index" aria-hidden="true">
                  0{i + 1}
                </span>
              </button>
            );
          })}
        </nav>
        <div className="sidebar-note">
          <div className="note-icon">
            <LockKeyhole size={19} />
          </div>
          <h3>Trust, in both directions.</h3>
          <p>
            Verify the caller.
            <br />
            Prove the agent.
            <br />
            Approve the exact action.
          </p>
          <Button variant="ghost" onClick={() => setAssistant(true)}>
            <MessageSquareText size={15} /> Ask a policy question{" "}
            <ArrowUpRight size={14} />
          </Button>
        </div>
        <div className="sidebar-bottom">
          <details className="staff-access">
            <summary>
              <KeyRound size={15} /> Staff access <ChevronRight size={14} />
            </summary>
            <label htmlFor="staffKey">Staff API key</label>
            <input
              id="staffKey"
              type="password"
              autoComplete="off"
              placeholder="Enter staff key"
            />
            <p>
              Kept in this browser session. Required for fraud-desk and research
              controls.
            </p>
          </details>
          <div className="sidebar-foot">
            <span className="demo-dot" /> SIMULATED BANKING <span>v1.0</span>
          </div>
        </div>
      </aside>
      <div className="main-shell" inert={compact && mobile}>
        <header className="app-topbar">
          <div className="breadcrumb">
            <button
              className="mobile-menu"
              aria-label="Open navigation"
              onClick={() => setMobile(true)}
            >
              <Menu size={20} />
            </button>
            <Layers3 size={17} />
            <span>Workspace</span>
            <ChevronRight size={14} />
            <strong>{current.label}</strong>
          </div>
          <div className="topbar-actions">
            <span className={"backend-state " + health}>
              <span />{" "}
              {health === "loading"
                ? "Connecting"
                : health === "online"
                  ? "Backend connected"
                  : "Connection unavailable"}
            </span>
            <Dialog>
              <DialogTrigger asChild>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label="About Nishabdha"
                >
                  <CircleHelp size={19} />
                </Button>
              </DialogTrigger>
              <DialogContent>
                <div className="dialog-symbol">
                  <ShieldCheck />
                </div>
                <DialogTitle>Trust before every action.</DialogTitle>
                <DialogDescription>
                  Nishabdha brings caller evidence, exact transaction approval,
                  and signed agent identity into one banking workflow.
                </DialogDescription>
                <div className="about-steps">
                  <p>
                    <AudioLines size={19} />
                    <span>
                      <b>Check the current voice request</b>Speaker and
                      authenticity evidence belongs to this utterance.
                    </span>
                  </p>
                  <p>
                    <Fingerprint size={19} />
                    <span>
                      <b>Approve the exact transaction</b>A passkey binds your
                      confirmation to the frozen recipient and amount.
                    </span>
                  </p>
                  <p>
                    <ShieldCheck size={19} />
                    <span>
                      <b>Verify the bank session</b>A signed record identifies
                      the application session. Telephone audio alone does not
                      establish this proof.
                    </span>
                  </p>
                </div>
                <p className="muted">
                  Banking actions are simulated. Live DetectifAI verification
                  requires configured vendor credentials.
                </p>
              </DialogContent>
            </Dialog>
            <span className="avatar">N</span>
          </div>
        </header>
        <div
          id="voiceModeBanner"
          className="mock-mode-banner"
          role="status"
          hidden
        />
        <main id="workspace">
          <div className="page-heading">
            <div>
              <div className="eyebrow">
                <span />
                {current.eyebrow}
              </div>
              <h1>{current.title}</h1>
              <p>{current.description}</p>
            </div>
            <div className="customer-selector">
              <label htmlFor="customer">DEMO CUSTOMER</label>
              <select
                id="customer"
                aria-label="Demo customer"
                defaultValue=""
              />
            </div>
          </div>
          <div className="assurance-strip">
            <div>
              <span className="step-number">01</span>
              <AudioLines size={20} />
              <p>
                <b>Caller evidence</b>
                <span>Current utterance checks</span>
              </p>
              <span className="step-tag">VOICE</span>
            </div>
            <div>
              <span className="step-number">02</span>
              <Fingerprint size={20} />
              <p>
                <b>Customer consent</b>
                <span>Exact action · Passkey approval</span>
              </p>
              <span className="step-tag">APP</span>
            </div>
            <div>
              <span className="step-number">03</span>
              <ShieldCheck size={20} />
              <p>
                <b>Controlled execution</b>
                <span>One action · One signed receipt</span>
              </p>
              <span className="step-tag">GATEWAY</span>
            </div>
          </div>
          <Workspace />
          <footer className="workspace-footer">
            <div>
              <span className="small-dot" /> Nishabdha{" "}
              <span className="footer-separator">/</span> Built for Unclone
              Agents
            </div>
            <span>Voice provider: {provider}</span>
          </footer>
        </main>
      </div>
      <Button
        className="assistant-launch"
        onClick={() => setAssistant(true)}
        aria-label="Open policy assistant"
      >
        <Sparkles size={17} />
        <span>Policy assistant</span>
        <ArrowUpRight size={15} />
      </Button>
      <Dialog open={assistant} onOpenChange={setAssistant}>
        <DialogContent className="assistant-dialog">
          <div className="assistant-heading">
            <span className="panel-icon">
              <BookOpen size={19} />
            </span>
            <div>
              <DialogTitle>Policy assistant</DialogTitle>
              <DialogDescription>
                Answers from reviewed Nishabdha policies
              </DialogDescription>
            </div>
          </div>
          <Suspense
            fallback={<div className="empty">Loading policy assistantâ€¦</div>}
          >
            <PolicyAssistant />
          </Suspense>
        </DialogContent>
      </Dialog>
      <AlertDialog
        open={!!transaction}
        onOpenChange={(open) => {
          if (!open) resolve(false);
        }}
      >
        <AlertDialogContent>
          <div className="dialog-symbol">
            <Fingerprint />
          </div>
          <AlertDialogTitle>Review this transfer</AlertDialogTitle>
          <AlertDialogDescription>
            Confirm these exact details before your passkey opens. If anything
            is wrong, cancel and start a new request.
          </AlertDialogDescription>
          {transaction && (
            <dl className="transaction-summary">
              <div>
                <dt>Amount</dt>
                <dd className="transaction-amount">
                  {new Intl.NumberFormat("en-IN", {
                    style: "currency",
                    currency: transaction.currency || "INR",
                  }).format(transaction.amount_paise / 100)}
                </dd>
              </div>
              <div>
                <dt>To</dt>
                <dd>
                  {transaction.payee_name}
                  <small>Account ending {transaction.destination_last4}</small>
                </dd>
              </div>
              <div>
                <dt>From</dt>
                <dd>Account ending {transaction.source_last4}</dd>
              </div>
            </dl>
          )}
          <div className="dialog-actions">
            <AlertDialogCancel asChild>
              <Button variant="outline" onClick={() => resolve(false)}>
                Cancel transfer
              </Button>
            </AlertDialogCancel>
            <AlertDialogAction asChild>
              <Button onClick={() => resolve(true)}>
                <Fingerprint size={17} /> Continue with passkey
              </Button>
            </AlertDialogAction>
          </div>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
