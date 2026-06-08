import { createContext, PropsWithChildren, useContext, useEffect, useMemo, useState } from "react";
import { api, getStoredToken, setStoredToken } from "../api/client";

export type AuthUser = {
  id: string;
  email: string;
  display_name: string;
  role: "admin" | "user";
  is_active: boolean;
};

type AuthContextValue = {
  user: AuthUser | null;
  loading: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
  reload: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: PropsWithChildren) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(Boolean(getStoredToken()));

  async function reload() {
    const token = getStoredToken();
    if (!token) {
      setUser(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const response = await api.get<AuthUser>("/auth/me");
      setUser(response.data);
    } catch {
      setStoredToken(null);
      setUser(null);
    } finally {
      setLoading(false);
    }
  }

  async function login(email: string, password: string) {
    const response = await api.post("/auth/login", { email, password });
    setStoredToken(response.data.access_token);
    setUser(response.data.user);
  }

  function logout() {
    setStoredToken(null);
    setUser(null);
  }

  useEffect(() => {
    void reload();
  }, []);

  const value = useMemo(() => ({ user, loading, login, logout, reload }), [user, loading]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const value = useContext(AuthContext);
  if (!value) {
    throw new Error("useAuth must be used within AuthProvider");
  }
  return value;
}
