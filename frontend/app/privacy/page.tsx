export default function Privacy() {
  return (
    <article className="plan card plain">
      <p className="eyebrow">PRIVACY & YOUR DATA</p>
      <h1>Your data</h1>
      <p>
        Eagles Away uses your trip details to plan an away day and shows the results in your
        browser. No account or marketing subscription is created.
      </p>
      <h2>Planning and saved results</h2>
      <p>
        We store your trip preferences, planning session and saved itinerary so the request can
        recover from interruptions. Our travel search provider receives the details needed to
        search. DigitalOcean hosts the app and database; Temporal runs its
        workflows. A necessary session cookie lets this browser access your saved plan. Your
        tab also keeps a copy of the brief so you can edit it after a search.
      </p>
      <p>
        We delete trip sessions and saved itineraries from our application database after 30 days.
        Travel prices and availability can change; check the provider before booking.
      </p>
      <h2>Request limits</h2>
      <p>
        To limit repeated requests, we store your IPv4 address or IPv6 network prefix with a
        count of attempts to start a plan. An hourly job deletes these records once the
        rate-limit window is at least an hour old.
      </p>
      <h2>Optional analytics</h2>
      <p>
        If you allow analytics, Google Analytics measures page views and whether the planning steps
        succeed. It uses the first-party cookies <code>_ga</code> and <code>_ga_*</code> to
        distinguish visits and sessions; their default lifetime is two years. Google also receives
        technical information from your browser. We send only page paths and general step names,
        not your trip details, prices or session ID. The Google tag loads only after you
        choose “Allow analytics.” You can change your choice using “Analytics choices” in the
        footer; rejecting or withdrawing consent stops tracking and clears these cookies. Read
        Google’s <a href="https://policies.google.com/privacy">privacy policy</a> for more about
        its processing.
      </p>
    </article>
  );
}
