"use client";

import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Browser speech-to-text for the mock interview page.
 *
 * Uses the Web Speech API (Chrome and Edge; not Firefox). It needs a secure
 * context, so open the dashboard at http://localhost:3000 or over HTTPS —
 * a plain-http LAN address will be refused the microphone.
 *
 * `continuous` mode still stops by itself after a stretch of silence, so the
 * hook restarts recognition until stop() is called: a candidate who pauses
 * to think is not cut off mid-answer.
 */
export function useSpeechRecognition({ lang = "en-IN" } = {}) {
  const [supported, setSupported] = useState(false);
  const [listening, setListening] = useState(false);
  const [finalText, setFinalText] = useState("");
  const [interimText, setInterimText] = useState("");
  const [error, setError] = useState(null);

  const recognitionRef = useRef(null);
  const wantListeningRef = useRef(false);
  const finalRef = useRef("");

  useEffect(() => {
    const SpeechRecognition =
      typeof window !== "undefined" && (window.SpeechRecognition || window.webkitSpeechRecognition);
    if (!SpeechRecognition) return undefined;
    setSupported(true);

    const recognition = new SpeechRecognition();
    recognition.lang = lang;
    recognition.continuous = true;
    recognition.interimResults = true;

    recognition.onresult = (event) => {
      let interim = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        const text = result[0]?.transcript || "";
        if (result.isFinal) {
          finalRef.current = `${finalRef.current} ${text}`.replace(/\s+/g, " ").trim();
        } else {
          interim += text;
        }
      }
      setFinalText(finalRef.current);
      setInterimText(interim.trim());
    };

    recognition.onerror = (event) => {
      if (event.error === "no-speech" || event.error === "aborted") return;
      if (event.error === "not-allowed" || event.error === "service-not-allowed") {
        wantListeningRef.current = false;
        setError("Microphone access is blocked. Allow the microphone for this site in your browser, or type your answer.");
      } else if (event.error === "audio-capture") {
        wantListeningRef.current = false;
        setError("No microphone was found. Connect one, or type your answer.");
      } else if (event.error === "network") {
        setError("Voice recognition needs an internet connection. You can type your answer instead.");
      } else {
        setError(`Voice recognition stopped (${event.error}). Press the microphone to try again.`);
      }
    };

    recognition.onend = () => {
      if (wantListeningRef.current) {
        try {
          recognition.start();
          return;
        } catch {
          // Fall through: report that listening stopped.
        }
      }
      wantListeningRef.current = false;
      setListening(false);
      setInterimText("");
    };

    recognitionRef.current = recognition;
    return () => {
      wantListeningRef.current = false;
      recognition.onresult = null;
      recognition.onerror = null;
      recognition.onend = null;
      try {
        recognition.abort();
      } catch {
        // Already stopped.
      }
    };
  }, [lang]);

  const start = useCallback(() => {
    const recognition = recognitionRef.current;
    if (!recognition) return;
    setError(null);
    wantListeningRef.current = true;
    try {
      recognition.start();
    } catch {
      // Already running — nothing to do.
    }
    setListening(true);
  }, []);

  const stop = useCallback(() => {
    wantListeningRef.current = false;
    try {
      recognitionRef.current?.stop();
    } catch {
      // Already stopped.
    }
    setListening(false);
  }, []);

  const reset = useCallback(() => {
    finalRef.current = "";
    setFinalText("");
    setInterimText("");
  }, []);

  /** Replace the transcript, e.g. when the candidate edits it by hand. */
  const setText = useCallback((text) => {
    finalRef.current = text;
    setFinalText(text);
    setInterimText("");
  }, []);

  return { supported, listening, finalText, interimText, error, start, stop, reset, setText };
}
