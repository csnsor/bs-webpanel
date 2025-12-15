-- Rolling message cache snapshots (last ~15 messages per user).
-- Used by the bot to persist message context even across restarts.

create table if not exists public.user_message_snapshots (
  user_id text not null,
  messages jsonb not null default '[]'::jsonb,
  updated_at bigint not null default (extract(epoch from now())::bigint),
  constraint user_message_snapshots_pkey primary key (user_id)
) tablespace pg_default;

-- If you created the table earlier without a default, fix the NOT NULL violation:
alter table public.user_message_snapshots
  alter column updated_at set default (extract(epoch from now())::bigint);

update public.user_message_snapshots
set updated_at = (extract(epoch from now())::bigint)
where updated_at is null;

-- Optional: auto-update `updated_at` on updates.
create or replace function public.tg_set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at := extract(epoch from now())::bigint;
  return new;
end;
$$;

drop trigger if exists set_user_message_snapshots_updated_at on public.user_message_snapshots;
create trigger set_user_message_snapshots_updated_at
before update on public.user_message_snapshots
for each row execute function public.tg_set_updated_at();
