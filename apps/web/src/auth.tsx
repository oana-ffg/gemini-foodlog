import {
  EmailAuthProvider,
  createUserWithEmailAndPassword,
  onAuthStateChanged,
  reauthenticateWithCredential,
  signInWithEmailAndPassword,
  signOut as firebaseSignOut,
  type User,
} from "firebase/auth";
import {
  createContext,
  type FormEvent,
  type ReactNode,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { auth } from "./firebase";

interface AuthContextValue {
  user: User | null;
  loading: boolean;
  signUp: (email: string, password: string) => Promise<void>;
  signIn: (email: string, password: string) => Promise<void>;
  signOut: () => Promise<void>;
  reauthenticate: (password: string) => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function authErrorMessage(error: unknown): string {
  if (!(error instanceof Error)) return "Authentication failed. Please try again.";

  const code = "code" in error && typeof error.code === "string" ? error.code : "";
  switch (code) {
    case "auth/email-already-in-use":
      return "That email already has an account. Sign in instead.";
    case "auth/invalid-credential":
    case "auth/wrong-password":
      return "The email or password is incorrect.";
    case "auth/invalid-email":
      return "Enter a valid email address.";
    case "auth/weak-password":
      return "Use a password with at least six characters.";
    case "auth/too-many-requests":
      return "Too many attempts. Wait a little and try again.";
    default:
      return error.message || "Authentication failed. Please try again.";
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(
    () => onAuthStateChanged(auth, (nextUser) => {
      setUser(nextUser);
      setLoading(false);
    }),
    [],
  );

  const value = useMemo<AuthContextValue>(() => ({
    user,
    loading,
    signUp: async (email, password) => {
      await createUserWithEmailAndPassword(auth, email, password);
    },
    signIn: async (email, password) => {
      await signInWithEmailAndPassword(auth, email, password);
    },
    signOut: async () => {
      await firebaseSignOut(auth);
    },
    reauthenticate: async (password) => {
      if (!user?.email) throw new Error("This account does not have a password email.");
      await reauthenticateWithCredential(
        user,
        EmailAuthProvider.credential(user.email, password),
      );
    },
  }), [loading, user]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}

export function AuthGate({ children }: { children: ReactNode }) {
  const { user, loading, signIn, signUp } = useAuth();
  const [mode, setMode] = useState<"sign-in" | "sign-up">("sign-in");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string>();

  if (loading) {
    return <main className="auth-shell"><p role="status">Checking your session…</p></main>;
  }

  if (user) return children;

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    setMessage(undefined);
    try {
      if (mode === "sign-in") {
        await signIn(email.trim(), password);
      } else {
        await signUp(email.trim(), password);
      }
      setPassword("");
    } catch (error: unknown) {
      setMessage(authErrorMessage(error));
      setBusy(false);
    }
  };

  const switchMode = () => {
    setMode((current) => current === "sign-in" ? "sign-up" : "sign-in");
    setPassword("");
    setMessage(undefined);
  };

  return (
    <main className="auth-shell">
      <section className="auth-card" aria-labelledby="auth-title">
        <p className="eyebrow">Gemini FoodLog</p>
        <h1 id="auth-title">{mode === "sign-in" ? "Welcome back." : "Create your account."}</h1>
        <p>Your kitchen images and journal stay private to your account.</p>
        <form onSubmit={submit} className="auth-form">
          <label>
            Email
            <input
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </label>
          <label>
            Password
            <input
              type="password"
              autoComplete={mode === "sign-in" ? "current-password" : "new-password"}
              required
              minLength={6}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>
          <button type="submit" disabled={busy}>
            {busy ? "Please wait…" : mode === "sign-in" ? "Sign in" : "Create account"}
          </button>
          {message ? <p className="form-message form-message--error" role="alert">{message}</p> : null}
        </form>
        <button type="button" className="text-button" onClick={switchMode} disabled={busy}>
          {mode === "sign-in" ? "Need an account? Sign up" : "Already registered? Sign in"}
        </button>
      </section>
    </main>
  );
}

export function SessionControls() {
  const { user, signOut, reauthenticate } = useAuth();
  const [showReauthentication, setShowReauthentication] = useState(false);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string>();

  const submitReauthentication = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    setMessage(undefined);
    try {
      await reauthenticate(password);
      setPassword("");
      setShowReauthentication(false);
      setMessage("Identity confirmed for sensitive actions.");
    } catch (error: unknown) {
      setMessage(authErrorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  const endSession = async () => {
    setBusy(true);
    setMessage(undefined);
    try {
      await signOut();
    } catch (error: unknown) {
      setMessage(authErrorMessage(error));
      setBusy(false);
    }
  };

  return (
    <aside className="session-controls" aria-label="Account session">
      <strong>{user?.email}</strong>
      <div className="session-controls__actions">
        <button
          type="button"
          className="text-button"
          disabled={busy}
          onClick={() => {
            setShowReauthentication((current) => !current);
            setPassword("");
            setMessage(undefined);
          }}
        >
          Reauthenticate
        </button>
        <button type="button" className="text-button" disabled={busy} onClick={endSession}>
          Sign out
        </button>
      </div>
      {showReauthentication ? (
        <form className="reauth-form" onSubmit={submitReauthentication}>
          <label>
            Confirm your password
            <input
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </label>
          <button type="submit" disabled={busy}>{busy ? "Checking…" : "Confirm identity"}</button>
        </form>
      ) : null}
      {message ? <p className="form-message" role="status">{message}</p> : null}
    </aside>
  );
}
