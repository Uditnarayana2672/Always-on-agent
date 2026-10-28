# Request: Entra ID app registration for a personal work assistant

Please create an app registration so a personal assistant on my laptop can read my own mail,
calendar, chats and Power BI refresh status, and act only after I approve each action.

- Name: Always-On Agent (personal)
- Supported accounts: this organisation only
- Platform: Mobile and desktop applications, redirect URI `http://localhost`
- Allow public client flows: Yes (device code sign-in)
- No client secret or certificate is needed (public client; tokens stay encrypted on my laptop with DPAPI)

Delegated Microsoft Graph permissions:
- Mail.ReadWrite
- Mail.Send
- Calendars.ReadWrite
- Chat.ReadWrite
- Sites.Read.All
- User.Read
- OnlineMeetingTranscript.Read.All (optional)

Delegated Power BI Service permissions:
- Dataset.ReadWrite.All
- Dataflow.ReadWrite.All
- Report.Read.All
- Workspace.Read.All

Please send back the Application (client) ID and the Directory (tenant) ID, and grant admin
consent if your policy requires it. All permissions are delegated: the app can only see what I can see.
