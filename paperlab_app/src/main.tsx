import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import "./styles.css";
import "./reading.css";
import "pdfjs-dist/web/pdf_viewer.css";

const client = new QueryClient({defaultOptions:{queries:{staleTime:15_000,retry:1}}});
ReactDOM.createRoot(document.getElementById("root")!).render(<React.StrictMode><QueryClientProvider client={client}><App/></QueryClientProvider></React.StrictMode>);
