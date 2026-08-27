import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import App from "./App";
import CameraPage from "./CameraPage";
import KnowledgePage from "./KnowledgePage";
import { AuthGate, AuthProvider } from "./auth";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AuthProvider>
      <AuthGate>
        <BrowserRouter>
          <Routes>
            <Route path="/" element={<App />} />
            <Route path="/camera" element={<CameraPage />} />
            <Route path="/knowledge" element={<KnowledgePage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </BrowserRouter>
      </AuthGate>
    </AuthProvider>
  </StrictMode>,
);
