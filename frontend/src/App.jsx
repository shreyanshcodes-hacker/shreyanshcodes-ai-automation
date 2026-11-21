import React, { useEffect, useState } from "react";

export default function App() {
  const [jobs, setJobs] = useState([]);
  const [ytStatus, setYtStatus] = useState(null);
  const [selectedJob, setSelectedJob] = useState(null);
  const [topic, setTopic] = useState("");
  const [format, setFormat] = useState("short");
  const [targetMinutes, setTargetMinutes] = useState(8);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  async function refresh() {
    try {
      const [jobsRes, ytRes] = await Promise.all([
        fetch("/api/jobs"),
        fetch("/api/youtube/status")
      ]);
      if (jobsRes.ok) {
        const data = await jobsRes.json();
        setJobs(data.jobs || []);
      }
      if (ytRes.ok) {
        setYtStatus(await ytRes.json());
      }
    } catch (err) {
      setError(String(err));
    }
  }

  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 3000);
    return () => clearInterval(timer);
  }, []);

  async function handleCreateJob(e) {
    e.preventDefault();
    if (!topic.trim()) return;
    setSubmitting(true);
    setError("");
    try {
      const res = await fetch("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          topic: topic.trim(),
          format,
          language: "Hinglish",
          target_minutes: Number(targetMinutes)
        })
      });
      if (!res.ok) throw new Error(await res.text());
      setTopic("");
      await refresh();
    } catch (err) {
      setError(String(err));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleCancel(id) {
    await fetch(`/api/jobs/${id}/cancel`, { method: "POST" });
    refresh();
  }

  async function handleRetry(id) {
    await fetch(`/api/jobs/${id}/retry`, { method: "POST" });
    refresh();
  }

  async function toggleKillSwitch() {
    if (!ytStatus) return;
    await fetch("/api/youtube/kill-switch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !ytStatus.kill_switch })
    });
    refresh();
  }

  return (
    <div className="container">
      <div className="header">
        <div>
          <h1 style={{ margin: 0 }}>Shreyansh Codes AI YouTube Studio</h1>
          <p style={{ margin: "4px 0 0", color: "var(--muted)" }}>
            Faceless Hinglish Production • 2 Shorts/day &amp; 2 Long/week • Private-First QA Gate
          </p>
        </div>
        {ytStatus && (
          <div className="actions">
            <span className="badge">
              YouTube: {ytStatus.connected ? "Connected" : "Not Connected"}
            </span>
            <button
              className={ytStatus.kill_switch ? "danger" : "secondary"}
              onClick={toggleKillSwitch}
            >
              Kill Switch: {ytStatus.kill_switch ? "ON" : "OFF"}
            </button>
          </div>
        )}
      </div>

      {error && (
        <div className="card" style={{ borderColor: "var(--danger)" }}>
          {error}
        </div>
      )}

      <div className="card">
        <h2 style={{ marginTop: 0 }}>Create Production Job</h2>
        <form className="form-grid" onSubmit={handleCreateJob}>
          <div>
            <label>Topic</label>
            <input
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              placeholder="e.g. FastAPI async background tasks in Hinglish"
              required
            />
          </div>
          <div>
            <label>Format</label>
            <select value={format} onChange={(e) => setFormat(e.target.value)}>
              <option value="short">Short (9:16)</option>
              <option value="long">Long Video (16:9)</option>
            </select>
          </div>
          <div>
            <label>Target Minutes (Long)</label>
            <input
              type="number"
              min="1"
              max="30"
              value={targetMinutes}
              onChange={(e) => setTargetMinutes(e.target.value)}
              disabled={format === "short"}
            />
          </div>
          <button type="submit" disabled={submitting}>
            {submitting ? "Queueing..." : "Start Job"}
          </button>
        </form>
      </div>

      <div className="card">
        <h2 style={{ marginTop: 0 }}>Production Jobs</h2>
        <table className="table">
          <thead>
            <tr>
              <th>Topic</th>
              <th>Format</th>
              <th>Status</th>
              <th>Stage</th>
              <th>Progress</th>
              <th>QA</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {jobs.map((job) => {
              const qaPassed = job.result?.qa_report?.passed;
              return (
                <tr key={job.id}>
                  <td>{job.topic}</td>
                  <td>{job.format}</td>
                  <td>
                    <span className={`badge ${job.status}`}>{job.status}</span>
                  </td>
                  <td>{job.stage}</td>
                  <td>
                    <div className="progress-track">
                      <div
                        className="progress-fill"
                        style={{ width: `${job.progress_pct || 0}%` }}
                      />
                    </div>
                  </td>
                  <td>
                    {qaPassed === true
                      ? "PASS"
                      : qaPassed === false
                      ? "FAIL"
                      : "—"}
                  </td>
                  <td className="actions">
                    {job.status === "completed" && (
                      <>
                        <button
                          className="secondary"
                          onClick={() => setSelectedJob(job)}
                        >
                          Preview
                        </button>
                        <a href={`/api/jobs/${job.id}/download`} download>
                          <button type="button" className="secondary">
                            Download
                          </button>
                        </a>
                      </>
                    )}
                    {(job.status === "running" || job.status === "queued") && (
                      <button
                        className="secondary"
                        onClick={() => handleCancel(job.id)}
                      >
                        Cancel
                      </button>
                    )}
                    {(job.status === "failed" || job.status === "cancelled") && (
                      <button
                        className="secondary"
                        onClick={() => handleRetry(job.id)}
                      >
                        Retry
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
            {jobs.length === 0 && (
              <tr>
                <td colSpan="7" style={{ color: "var(--muted)" }}>
                  No jobs yet. Create a job above to start producing.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {selectedJob && (
        <div className="card">
          <div className="header" style={{ marginBottom: 12 }}>
            <h2 style={{ margin: 0 }}>Preview: {selectedJob.topic}</h2>
            <button className="secondary" onClick={() => setSelectedJob(null)}>
              Close
            </button>
          </div>
          <video controls src={`/api/jobs/${selectedJob.id}/preview`} />
          {selectedJob.result?.qa_report && (
            <pre
              style={{
                marginTop: 12,
                padding: 12,
                background: "#0d131d",
                borderRadius: 8,
                overflowX: "auto",
                fontSize: 12
              }}
            >
              {JSON.stringify(selectedJob.result.qa_report, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
