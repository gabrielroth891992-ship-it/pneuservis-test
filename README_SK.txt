PNEUSERVIS TEST — DEMO

Toto je samostatná pneuservisná verzia. Existujúci Autoservis Test nemeň.

Dôležité:
- Tento kód používa samostatnú PostgreSQL tabuľku `tire_service_leads`.
- Môže zdieľať existujúci Neon projekt s autoservisom, ale dopyty budú v inej tabuľke.
- Pred verejným nasadením vytvor samostatný GitHub repozitár a samostatnú Render Web Service.
- V novej Render službe nastav Environment Variables podľa .env.example.
- DATABASE_URL môže byť rovnaký Neon connection string ako pre Autoservis Test, pretože tabuľky sú oddelené.
- Nikdy nenahrávaj skutočný .env ani tajné kľúče na GitHub.
- Fiktívny názov/adresa/telefón v ukážke treba nahradiť údajmi konkrétnej prevádzky až po súhlase jej majiteľa.
- Dopyty sa ukladajú do PostgreSQL, do tabuľky `tire_service_leads`.
- Admin používa Basic Auth cez ADMIN_USERNAME a ADMIN_PASSWORD.
