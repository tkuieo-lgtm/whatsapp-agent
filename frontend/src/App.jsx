import { NavLink, Route, Routes } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import Rules from "./pages/Rules";
import Logs from "./pages/Logs";

const NAV = [
  { to: "/", label: "לוח בקרה" },
  { to: "/rules", label: "חוקי מייל" },
  { to: "/logs", label: "לוג פעולות" },
];

export default function App() {
  return (
    <div className="min-h-screen bg-gray-50 text-gray-900" dir="rtl">
      <nav className="bg-green-600 text-white px-6 py-3 flex items-center gap-6 shadow">
        <span className="font-bold text-lg">🤖 WhatsApp AI Agent</span>
        <div className="flex gap-4 mr-auto">
          {NAV.map(({ to, label }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={({ isActive }) =>
                `px-3 py-1 rounded transition ${
                  isActive ? "bg-white text-green-700 font-semibold" : "hover:bg-green-500"
                }`
              }
            >
              {label}
            </NavLink>
          ))}
        </div>
      </nav>

      <main className="max-w-5xl mx-auto px-4 py-8">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/rules" element={<Rules />} />
          <Route path="/logs" element={<Logs />} />
        </Routes>
      </main>
    </div>
  );
}
