import { getApp, getApps, initializeApp } from "firebase/app";
import { getAuth } from "firebase/auth";

// Firebase web configuration identifies this public client; it is not a
// server credential. The API key is restricted in GCP to the FoodLog hosting
// domains, localhost, and the Firebase APIs used by this application.
const firebaseConfig = {
  apiKey: "AIzaSyD9EXb0ryVkZ95JpCUG0TpOLBYZnhk_vhM",
  authDomain: "gemini-foodlog-2026.firebaseapp.com",
  projectId: "gemini-foodlog-2026",
  storageBucket: "gemini-foodlog-2026.firebasestorage.app",
  messagingSenderId: "163029863855",
  appId: "1:163029863855:web:e355d979c7bdf4044eb35a",
} as const;

const firebaseApp = getApps().length === 0 ? initializeApp(firebaseConfig) : getApp();

export const auth = getAuth(firebaseApp);
