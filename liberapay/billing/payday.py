# coding: utf8

from __future__ import print_function, unicode_literals

from datetime import date
from decimal import Decimal, ROUND_UP
import os
import os.path
from subprocess import Popen
import sys

from babel.dates import format_timedelta
import pando.utils
import requests

from liberapay import constants
from liberapay.billing.transactions import transfer
from liberapay.exceptions import NegativeBalance
from liberapay.models.participant import Participant
from liberapay.utils import NS, group_by
from liberapay.website import website


log = print


def round_up(d):
    return d.quantize(constants.D_CENT, rounding=ROUND_UP)


class NoPayday(Exception):
    __str__ = lambda self: "No payday found where one was expected."


class Payday(object):

    @classmethod
    def start(cls, public_log=''):
        """Try to start a new Payday.

        If there is a Payday that hasn't finished yet, then the UNIQUE
        constraint on ts_end will kick in and notify us of that. In that case
        we load the existing Payday and work on it some more. We use the start
        time of the current Payday to synchronize our work.

        """
        d = cls.db.one("""
            INSERT INTO paydays
                        (id, public_log, ts_start)
                 VALUES ( COALESCE((SELECT id FROM paydays ORDER BY id DESC LIMIT 1), 0) + 1
                        , %s
                        , now()
                        )
            ON CONFLICT (ts_end) DO UPDATE
                    SET ts_start = COALESCE(paydays.ts_start, excluded.ts_start)
              RETURNING id, (ts_start AT TIME ZONE 'UTC') AS ts_start, stage
        """, (public_log,), back_as=dict)
        log("Running payday #%s." % d['id'])

        d['ts_start'] = d['ts_start'].replace(tzinfo=pando.utils.utc)

        log("Payday started at %s." % d['ts_start'])

        payday = Payday()
        payday.__dict__.update(d)
        return payday

    def run(self, log_dir='.', keep_log=False, recompute_stats=10, update_cached_amounts=True):
        """This is the starting point for payday.

        It is structured such that it can be run again safely (with a
        newly-instantiated Payday object) if it crashes.

        """
        self.db.self_check()

        _start = pando.utils.utcnow()
        log("Greetings, program! It's PAYDAY!!!!")

        self.shuffle(log_dir)

        self.end()

        self.recompute_stats(limit=recompute_stats)
        if update_cached_amounts:
            self.update_cached_amounts()

        self.notify_participants()

        _end = pando.utils.utcnow()
        _delta = _end - _start
        msg = "Script ran for %s ({0})."
        log(msg.format(_delta) % format_timedelta(_delta, locale='en'))

        if keep_log:
            output_log_name = 'payday-%i.txt' % self.id
            output_log_path = log_dir+'/'+output_log_name
            if website.s3:
                s3_bucket = website.app_conf.s3_payday_logs_bucket
                s3_key = 'paydays/'+output_log_name
                website.s3.upload_file(output_log_path+'.part', s3_bucket, s3_key)
                log("Uploaded log to S3.")
            os.rename(output_log_path+'.part', output_log_path)

        self.db.run("UPDATE paydays SET stage = NULL WHERE id = %s", (self.id,))

    def shuffle(self, log_dir='.'):
        if self.stage > 2:
            return
        get_transfers = lambda: [NS(t._asdict()) for t in self.db.all("""
            SELECT t.*
                 , p.mangopay_user_id AS tipper_mango_id
                 , p2.mangopay_user_id AS tippee_mango_id
                 , p.mangopay_wallet_id AS tipper_wallet_id
                 , p2.mangopay_wallet_id AS tippee_wallet_id
              FROM payday_transfers t
              JOIN participants p ON p.id = t.tipper
              JOIN participants p2 ON p2.id = t.tippee
          ORDER BY t.id
        """)]
        if self.stage == 2:
            transfers = get_transfers()
            done = self.db.all("""
                SELECT *
                  FROM transfers t
                 WHERE t.timestamp >= %(ts_start)s;
            """, dict(ts_start=self.ts_start))
            done = set((t.tipper, t.tippee, t.context, t.team) for t in done)
            transfers = [t for t in transfers if (t.tipper, t.tippee, t.context, t.team) not in done]
        else:
            assert self.stage == 1
            with self.db.get_cursor() as cursor:
                self.prepare(cursor, self.ts_start)
                self.transfer_virtually(cursor, self.ts_start)
                self.check_balances(cursor)
                cursor.run("""
                    UPDATE paydays
                       SET nparticipants = (SELECT count(*) FROM payday_participants)
                     WHERE ts_end='1970-01-01T00:00:00+00'::timestamptz;
                """)
            self.clean_up()
            self.mark_stage_done()
            transfers = get_transfers()

        self.transfer_for_real(transfers)
        self.settle_debts(self.db)

        self.db.self_check()
        self.mark_stage_done()
        self.db.run("DROP TABLE payday_transfers")

    @staticmethod
    def prepare(cursor, ts_start):
        """Prepare the DB: we need temporary tables with indexes and triggers.
        """
        cursor.run("""

        -- Create the necessary temporary tables and indexes

        CREATE TEMPORARY TABLE payday_participants ON COMMIT DROP AS
            SELECT id
                 , username
                 , join_time
                 , balance AS old_balance
                 , balance AS new_balance
                 , goal
                 , kind
              FROM participants p
             WHERE join_time < %(ts_start)s
               AND (mangopay_user_id IS NOT NULL OR kind = 'group')
               AND is_suspended IS NOT true
               AND status = 'active'
          ORDER BY join_time;

        CREATE UNIQUE INDEX ON payday_participants (id);

        CREATE TEMPORARY TABLE payday_tips ON COMMIT DROP AS
            SELECT t.id, tipper, tippee, amount, (p2.kind = 'group') AS to_team
              FROM ( SELECT DISTINCT ON (tipper, tippee) *
                       FROM tips
                      WHERE mtime < %(ts_start)s
                   ORDER BY tipper, tippee, mtime DESC
                   ) t
              JOIN payday_participants p ON p.id = t.tipper
              JOIN payday_participants p2 ON p2.id = t.tippee
             WHERE t.amount > 0
               AND (p2.goal IS NULL or p2.goal >= 0)
          ORDER BY p.join_time ASC, t.ctime ASC;

        CREATE INDEX ON payday_tips (tipper);
        CREATE INDEX ON payday_tips (tippee);
        ALTER TABLE payday_tips ADD COLUMN is_funded boolean;

        CREATE TEMPORARY TABLE payday_takes ON COMMIT DROP AS
            SELECT team, member, amount
              FROM ( SELECT DISTINCT ON (team, member)
                            team, member, amount
                       FROM takes
                      WHERE mtime < %(ts_start)s
                   ORDER BY team, member, mtime DESC
                   ) t
             WHERE t.amount IS NOT NULL
               AND t.amount > 0
               AND t.team IN (SELECT id FROM payday_participants)
               AND t.member IN (SELECT id FROM payday_participants);

        CREATE UNIQUE INDEX ON payday_takes (team, member);

        DROP TABLE IF EXISTS payday_transfers;
        CREATE TABLE payday_transfers
        ( id serial
        , tipper bigint
        , tippee bigint
        , amount numeric(35,2)
        , context transfer_context
        , team bigint
        , invoice int
        , UNIQUE (tipper, tippee, context, team)
        );


        -- Prepare a statement that makes and records a transfer

        CREATE OR REPLACE FUNCTION transfer(bigint, bigint, numeric, transfer_context, bigint, int)
        RETURNS void AS $$
            BEGIN
                IF ($3 = 0) THEN RETURN; END IF;
                UPDATE payday_participants
                   SET new_balance = (new_balance - $3)
                 WHERE id = $1;
                IF (NOT FOUND) THEN RAISE 'tipper %% not found', $1; END IF;
                UPDATE payday_participants
                   SET new_balance = (new_balance + $3)
                 WHERE id = $2;
                IF (NOT FOUND) THEN RAISE 'tippee %% not found', $2; END IF;
                INSERT INTO payday_transfers
                            (tipper, tippee, amount, context, team, invoice)
                     VALUES ($1, $2, $3, $4, $5, $6);
            END;
        $$ LANGUAGE plpgsql;


        -- Create a trigger to process tips

        CREATE OR REPLACE FUNCTION process_tip() RETURNS trigger AS $$
            DECLARE
                tipper payday_participants;
            BEGIN
                tipper := (
                    SELECT p.*::payday_participants
                      FROM payday_participants p
                     WHERE id = NEW.tipper
                );
                IF (NEW.amount <= tipper.new_balance) THEN
                    EXECUTE transfer(NEW.tipper, NEW.tippee, NEW.amount, 'tip', NULL, NULL);
                    RETURN NEW;
                END IF;
                RETURN NULL;
            END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER process_tip BEFORE UPDATE OF is_funded ON payday_tips
            FOR EACH ROW
            WHEN (NEW.is_funded IS true AND OLD.is_funded IS NOT true AND NEW.to_team IS NOT true)
            EXECUTE PROCEDURE process_tip();


        -- Create a function to settle one-to-one donations

        CREATE OR REPLACE FUNCTION settle_tip_graph() RETURNS void AS $$
            DECLARE
                count integer NOT NULL DEFAULT 0;
                i integer := 0;
            BEGIN
                LOOP
                    i := i + 1;
                    WITH updated_rows AS (
                         UPDATE payday_tips
                            SET is_funded = true
                          WHERE is_funded IS NOT true
                            AND to_team IS NOT true
                      RETURNING *
                    )
                    SELECT COUNT(*) FROM updated_rows INTO count;
                    IF (count = 0) THEN
                        EXIT;
                    END IF;
                    IF (i > 50) THEN
                        RAISE 'Reached the maximum number of iterations';
                    END IF;
                END LOOP;
            END;
        $$ LANGUAGE plpgsql;

        """, dict(ts_start=ts_start))
        log("Prepared the DB.")

    @staticmethod
    def transfer_virtually(cursor, ts_start):
        cursor.run("SELECT settle_tip_graph();")
        teams = cursor.all("""
            SELECT id FROM payday_participants WHERE kind = 'group';
        """)
        for team_id in teams:
            Payday.resolve_takes(cursor, team_id)
        cursor.run("""
            SELECT settle_tip_graph();
            UPDATE payday_tips SET is_funded = false WHERE is_funded IS NULL;
        """)
        Payday.pay_invoices(cursor, ts_start)

    @staticmethod
    def resolve_takes(cursor, team_id):
        """Resolve many-to-many donations (team takes)
        """
        args = dict(team_id=team_id)
        total_income = cursor.one("""
            WITH funded AS (
                 UPDATE payday_tips
                    SET is_funded = true
                   FROM payday_participants p
                  WHERE p.id = tipper
                    AND tippee = %(team_id)s
                    AND p.new_balance >= amount
              RETURNING amount
            )
            SELECT COALESCE(sum(amount), 0) FROM funded;
        """, args)
        total_takes = cursor.one("""
            SELECT COALESCE(sum(t.amount), 0)
              FROM payday_takes t
             WHERE t.team = %(team_id)s
        """, args)
        if total_income == 0 or total_takes == 0:
            return
        args['takes_ratio'] = min(total_income / total_takes, 1)
        tips_ratio = args['tips_ratio'] = min(total_takes / total_income, 1)
        tips = [NS(t._asdict()) for t in cursor.all("""
            SELECT t.id, t.tipper, (round_up(t.amount * %(tips_ratio)s, 2)) AS amount
                 , t.amount AS full_amount
                 , COALESCE((
                       SELECT sum(tr.amount)
                         FROM transfers tr
                        WHERE tr.tipper = t.tipper
                          AND tr.team = %(team_id)s
                          AND tr.context = 'take'
                          AND tr.status = 'succeeded'
                   ), 0) AS past_transfers_sum
              FROM payday_tips t
              JOIN payday_participants p ON p.id = t.tipper
             WHERE t.tippee = %(team_id)s
               AND p.new_balance >= t.amount
        """, args)]
        takes = [NS(t._asdict()) for t in cursor.all("""
            SELECT t.member, (round_up(t.amount * %(takes_ratio)s, 2)) AS amount
              FROM payday_takes t
             WHERE t.team = %(team_id)s;
        """, args)]
        adjust_tips = tips_ratio != 1
        if adjust_tips:
            # The team has a leftover, so donation amounts can be adjusted.
            # In the following loop we compute the "weeks" count of each tip.
            # For example the `weeks` value is 2.5 for a donation currently at
            # 10€/week which has distributed 25€ in the past.
            for tip in tips:
                tip.weeks = round_up(tip.past_transfers_sum / tip.full_amount)
            max_weeks = max(tip.weeks for tip in tips)
            min_weeks = min(tip.weeks for tip in tips)
            adjust_tips = max_weeks != min_weeks
            if adjust_tips:
                # Some donors have given fewer weeks worth of money than others,
                # we want to adjust the amounts so that the weeks count will
                # eventually be the same for every donation.
                min_tip_ratio = tips_ratio * Decimal('0.1')
                # Loop: compute how many "weeks" each tip is behind the "oldest"
                # tip, as well as a naive ratio and amount based on that number
                # of weeks
                for tip in tips:
                    tip.weeks_to_catch_up = max_weeks - tip.weeks
                    tip.ratio = min(min_tip_ratio + tip.weeks_to_catch_up, 1)
                    tip.amount = round_up(tip.full_amount * tip.ratio)
                naive_amounts_sum = sum(tip.amount for tip in tips)
                total_to_transfer = min(total_takes, total_income)
                delta = total_to_transfer - naive_amounts_sum
                if delta == 0:
                    # The sum of the naive amounts computed in the previous loop
                    # matches the end target, we got very lucky and no further
                    # adjustments are required
                    adjust_tips = False
                else:
                    # Loop: compute the "leeway" of each tip, i.e. how much it
                    # can be increased or decreased to fill the `delta` gap
                    if delta < 0:
                        # The naive amounts are too high: we want to lower the
                        # amounts of the tips that have a "high" ratio, leaving
                        # untouched the ones that are already low
                        for tip in tips:
                            if tip.ratio > min_tip_ratio:
                                min_tip_amount = round_up(tip.full_amount * min_tip_ratio)
                                tip.leeway = min_tip_amount - tip.amount
                            else:
                                tip.leeway = 0
                    else:
                        # The naive amounts are too low: we can raise all the
                        # tips that aren't already at their maximum
                        for tip in tips:
                            tip.leeway = tip.full_amount - tip.amount
                    leeway = sum(tip.leeway for tip in tips)
                    leeway_ratio = min(delta / leeway, 1)
                    tips = sorted(tips, key=lambda tip: (-tip.weeks_to_catch_up, tip.id))
        # Loop: compute the adjusted donation amounts, and do the transfers
        for tip in tips:
            if adjust_tips:
                tip_amount = round_up(tip.amount + tip.leeway * leeway_ratio)
                if tip_amount == 0:
                    continue
                assert tip_amount > 0
                assert tip_amount <= tip.full_amount
                tip.amount = tip_amount
            for take in takes:
                if take.amount == 0 or tip.tipper == take.member:
                    continue
                transfer_amount = min(tip.amount, take.amount)
                cursor.run("SELECT transfer(%s, %s, %s, 'take', %s, NULL)",
                           (tip.tipper, take.member, transfer_amount, team_id))
                tip.amount -= transfer_amount
                take.amount -= transfer_amount
                if tip.amount == 0:
                    break

    @staticmethod
    def pay_invoices(cursor, ts_start):
        """Settle pending invoices
        """
        invoices = cursor.all("""
            SELECT i.*
              FROM invoices i
             WHERE i.status = 'accepted'
               AND ( SELECT ie.ts
                       FROM invoice_events ie
                      WHERE ie.invoice = i.id
                   ORDER BY ts DESC
                      LIMIT 1
                   ) < %(ts_start)s;
        """, dict(ts_start=ts_start))
        for i in invoices:
            payer_balance = cursor.one("""
                SELECT p.new_balance
                  FROM payday_participants p
                 WHERE id = %s
            """, (i.addressee,))
            if payer_balance < i.amount:
                continue
            cursor.run("""
                SELECT transfer(%(addressee)s, %(sender)s, %(amount)s,
                                %(nature)s::transfer_context, NULL, %(id)s);
                UPDATE invoices
                   SET status = 'paid'
                 WHERE id = %(id)s;
                INSERT INTO invoice_events
                            (invoice, participant, status)
                     VALUES (%(id)s, %(addressee)s, 'paid');
            """, i._asdict())

    @staticmethod
    def check_balances(cursor):
        """Check that balances aren't becoming (more) negative
        """
        oops = cursor.one("""
            SELECT *
              FROM (
                     SELECT p.id
                          , p.username
                          , (p.balance + p2.new_balance - p2.old_balance) AS new_balance
                          , p.balance AS cur_balance
                       FROM payday_participants p2
                       JOIN participants p ON p.id = p2.id
                        AND p2.new_balance <> p2.old_balance
                   ) foo
             WHERE new_balance < 0 AND new_balance < cur_balance
             LIMIT 1
        """)
        if oops:
            log(oops)
            raise NegativeBalance()
        log("Checked the balances.")

    def transfer_for_real(self, transfers):
        db = self.db
        print("Starting transfers (n=%i)" % len(transfers))
        msg = "Executing transfer #%i (amount=%s context=%s team=%s tipper_wallet_id=%s tippee_wallet_id=%s)"
        for t in transfers:
            log(msg % (t.id, t.amount, t.context, t.team, t.tipper_wallet_id, t.tippee_wallet_id))
            transfer(db, **t.__dict__)

    def clean_up(self):
        self.db.run("""
            DROP FUNCTION process_tip();
            DROP FUNCTION settle_tip_graph();
            DROP FUNCTION transfer(bigint, bigint, numeric, transfer_context, bigint, int);
        """)

    @staticmethod
    def settle_debts(db):
        while True:
            with db.get_cursor() as cursor:
                debt = cursor.one("""
                    SELECT d.id, d.debtor AS tipper, d.creditor AS tippee, d.amount
                         , 'debt' AS context
                         , p_debtor.mangopay_user_id AS tipper_mango_id
                         , p_debtor.mangopay_wallet_id AS tipper_wallet_id
                         , p_creditor.mangopay_user_id AS tippee_mango_id
                         , p_creditor.mangopay_wallet_id AS tippee_wallet_id
                      FROM debts d
                      JOIN participants p_debtor ON p_debtor.id = d.debtor
                      JOIN participants p_creditor ON p_creditor.id = d.creditor
                     WHERE d.status = 'due'
                       AND p_debtor.balance >= d.amount
                       AND p_creditor.status = 'active'
                     LIMIT 1
                       FOR UPDATE OF d
                """)
                if not debt:
                    break
                try:
                    t_id = transfer(db, **debt._asdict())[1]
                except NegativeBalance:
                    continue
                cursor.run("""
                    UPDATE debts
                       SET status = 'paid'
                         , settlement = %s
                     WHERE id = %s
                """, (t_id, debt.id))

    @classmethod
    def update_stats(cls, payday_id):
        ts_start, ts_end = cls.db.one("""
            SELECT ts_start, ts_end FROM paydays WHERE id = %s
        """, (payday_id,))
        if payday_id > 1:
            previous_ts_start = cls.db.one("""
                SELECT ts_start
                  FROM paydays
                 WHERE id = %s
            """, (payday_id - 1,))
        else:
            previous_ts_start = constants.EPOCH
        assert previous_ts_start
        cls.db.run("""\

            WITH our_transfers AS (
                     SELECT *
                       FROM transfers
                      WHERE "timestamp" >= %(ts_start)s
                        AND "timestamp" <= %(ts_end)s
                        AND status = 'succeeded'
                        AND context IN ('tip', 'take')
                 )
               , our_tips AS (
                     SELECT *
                       FROM our_transfers
                      WHERE context = 'tip'
                 )
               , our_takes AS (
                     SELECT *
                       FROM our_transfers
                      WHERE context = 'take'
                 )
               , week_exchanges AS (
                     SELECT e.*
                          , ( EXISTS (
                                SELECT e2.id
                                  FROM exchanges e2
                                 WHERE e2.refund_ref = e.id
                            )) AS refunded
                       FROM exchanges e
                      WHERE e.timestamp < %(ts_start)s
                        AND e.timestamp >= %(previous_ts_start)s
                        AND status <> 'failed'
                 )
            UPDATE paydays
               SET nactive = (
                       SELECT DISTINCT count(*) FROM (
                           SELECT tipper FROM our_transfers
                               UNION
                           SELECT tippee FROM our_transfers
                       ) AS foo
                   )
                 , ntippers = (SELECT count(DISTINCT tipper) FROM our_transfers)
                 , ntippees = (SELECT count(DISTINCT tippee) FROM our_transfers)
                 , ntips = (SELECT count(*) FROM our_tips)
                 , ntakes = (SELECT count(*) FROM our_takes)
                 , take_volume = (SELECT COALESCE(sum(amount), 0) FROM our_takes)
                 , ntransfers = (SELECT count(*) FROM our_transfers)
                 , transfer_volume = (SELECT COALESCE(sum(amount), 0) FROM our_transfers)
                 , transfer_volume_refunded = (
                       SELECT COALESCE(sum(amount), 0)
                         FROM our_transfers
                        WHERE refund_ref IS NOT NULL
                   )
                 , nusers = (
                       SELECT count(*)
                         FROM participants p
                        WHERE p.kind IN ('individual', 'organization')
                          AND p.join_time < %(ts_start)s
                          AND COALESCE((
                                SELECT payload::text
                                  FROM events e
                                 WHERE e.participant = p.id
                                   AND e.type = 'set_status'
                                   AND e.ts < %(ts_start)s
                              ORDER BY ts DESC
                                 LIMIT 1
                              ), '') <> '"closed"'
                   )
                 , week_deposits = (
                       SELECT COALESCE(sum(amount), 0)
                         FROM week_exchanges
                        WHERE amount > 0
                          AND refund_ref IS NULL
                          AND status = 'succeeded'
                   )
                 , week_deposits_refunded = (
                       SELECT COALESCE(sum(amount), 0)
                         FROM week_exchanges
                        WHERE amount > 0
                          AND refunded
                   )
                 , week_withdrawals = (
                       SELECT COALESCE(-sum(amount), 0)
                         FROM week_exchanges
                        WHERE amount < 0
                          AND refund_ref IS NULL
                   )
                 , week_withdrawals_refunded = (
                       SELECT COALESCE(sum(amount), 0)
                         FROM week_exchanges
                        WHERE amount < 0
                          AND refunded
                   )
             WHERE id = %(payday_id)s

        """, locals())
        log("Updated stats of payday #%i." % payday_id)

    @classmethod
    def recompute_stats(cls, limit=None):
        ids = cls.db.all("""
            SELECT id
              FROM paydays
             WHERE ts_end > ts_start
          ORDER BY id DESC
             LIMIT %s
        """, (limit,))
        for payday_id in ids:
            cls.update_stats(payday_id)

    def update_cached_amounts(self):
        now = pando.utils.utcnow()
        with self.db.get_cursor() as cursor:
            self.prepare(cursor, now)
            self.transfer_virtually(cursor, now)
            cursor.run("""

            UPDATE tips t
               SET is_funded = t2.is_funded
              FROM payday_tips t2
             WHERE t.id = t2.id
               AND t.is_funded <> t2.is_funded;

            UPDATE participants p
               SET giving = p2.giving
              FROM ( SELECT p2.id
                          , COALESCE((
                                SELECT sum(amount)
                                  FROM payday_tips t
                                 WHERE t.tipper = p2.id
                                   AND t.is_funded
                            ), 0) AS giving
                       FROM participants p2
                   ) p2
             WHERE p.id = p2.id
               AND p.giving <> p2.giving;

            UPDATE participants p
               SET taking = p2.taking
              FROM ( SELECT p2.id
                          , COALESCE((
                                SELECT sum(amount)
                                  FROM payday_transfers t
                                 WHERE t.tippee = p2.id
                                   AND context = 'take'
                            ), 0) AS taking
                       FROM participants p2
                   ) p2
             WHERE p.id = p2.id
               AND p.taking <> p2.taking;

            UPDATE participants p
               SET receiving = p2.receiving
              FROM ( SELECT p2.id
                          , p2.taking + COALESCE((
                                SELECT sum(amount)
                                  FROM payday_tips t
                                 WHERE t.tippee = p2.id
                                   AND t.is_funded
                            ), 0) AS receiving
                       FROM participants p2
                   ) p2
             WHERE p.id = p2.id
               AND p.receiving <> p2.receiving
               AND p.status <> 'stub';

            UPDATE participants p
               SET npatrons = p2.npatrons
              FROM ( SELECT p2.id
                          , ( SELECT count(*)
                                FROM payday_transfers t
                               WHERE t.tippee = p2.id
                            ) AS npatrons
                       FROM participants p2
                   ) p2
             WHERE p.id = p2.id
               AND p.npatrons <> p2.npatrons
               AND p.status <> 'stub'
               AND p.kind IN ('individual', 'organization');

            UPDATE participants p
               SET npatrons = p2.npatrons
              FROM ( SELECT p2.id
                          , ( SELECT count(*)
                                FROM payday_tips t
                               WHERE t.tippee = p2.id
                                 AND t.is_funded
                            ) AS npatrons
                       FROM participants p2
                   ) p2
             WHERE p.id = p2.id
               AND p.npatrons <> p2.npatrons
               AND p.kind = 'group';

            """)
        self.clean_up()
        log("Updated receiving amounts.")

    def mark_stage_done(self):
        self.stage = self.db.one("""
            UPDATE paydays
               SET stage = stage + 1
             WHERE id = %s
         RETURNING stage
        """, (self.id,), default=NoPayday)

    def end(self):
        self.ts_end = self.db.one("""
            UPDATE paydays
               SET ts_end=now()
             WHERE ts_end='1970-01-01T00:00:00+00'::timestamptz
         RETURNING ts_end AT TIME ZONE 'UTC'
        """, default=NoPayday).replace(tzinfo=pando.utils.utc)

    def notify_participants(self):
        previous_ts_end = self.db.one("""
            SELECT ts_end
              FROM paydays
             WHERE ts_start < %s
          ORDER BY ts_end DESC
             LIMIT 1
        """, (self.ts_start,), default=constants.BIRTHDAY)

        # Income notifications
        r = self.db.all("""
            SELECT tippee, json_agg(t) AS transfers
              FROM transfers t
             WHERE "timestamp" > %s
               AND "timestamp" <= %s
               AND context IN ('tip', 'take', 'final-gift')
          GROUP BY tippee
        """, (previous_ts_end, self.ts_end))
        for tippee_id, transfers in r:
            successes = [t for t in transfers if t['status'] == 'succeeded']
            if not successes:
                continue
            by_team = {k: sum(t['amount'] for t in v)
                       for k, v in group_by(successes, 'team').items()}
            personal = by_team.pop(None, 0)
            by_team = {Participant.from_id(k).username: v for k, v in by_team.items()}
            p = Participant.from_id(tippee_id)
            p.notify(
                'income',
                total=sum(t['amount'] for t in successes),
                personal=personal,
                by_team=by_team,
                new_balance=p.balance,
            )

        # Identity-required notifications
        participants = self.db.all("""
            SELECT p.*::participants
              FROM participants p
             WHERE mangopay_user_id IS NULL
               AND kind IN ('individual', 'organization')
               AND (p.goal IS NULL OR p.goal >= 0)
               AND EXISTS (
                     SELECT 1
                       FROM current_tips t
                       JOIN participants p2 ON p2.id = t.tipper
                      WHERE t.tippee = p.id
                        AND t.amount > 0
                        AND p2.balance > t.amount
                   )
        """)
        for p in participants:
            p.notify('identity_required', force_email=True)

        # Low-balance notifications
        participants = self.db.all("""
            SELECT p.*::participants
              FROM participants p
             WHERE balance < (
                     SELECT sum(amount)
                       FROM current_tips t
                       JOIN participants p2 ON p2.id = t.tippee
                      WHERE t.tipper = p.id
                        AND p2.mangopay_user_id IS NOT NULL
                        AND p2.status = 'active'
                        AND p2.is_suspended IS NOT true
                   )
               AND EXISTS (
                     SELECT 1
                       FROM transfers t
                      WHERE t.tipper = p.id
                        AND t.timestamp > %s
                        AND t.timestamp <= %s
                        AND t.status = 'succeeded'
                   )
        """, (previous_ts_end, self.ts_end))
        for p in participants:
            p.notify('low_balance', low_balance=p.balance)


def create_payday_issue():
    # Make sure today is payday
    today = date.today()
    today_weekday = today.isoweekday()
    today_is_wednesday = today_weekday == 3
    assert today_is_wednesday, today_weekday
    # Fetch last payday from DB
    last_payday = website.db.one("SELECT * FROM paydays ORDER BY id DESC LIMIT 1")
    if last_payday:
        last_start = last_payday.ts_start
        if last_start is None or last_start.date() == today:
            return
    next_payday_id = str(last_payday.id + 1 if last_payday else 1)
    # Prepare to make API requests
    app_conf = website.app_conf
    sess = requests.Session()
    sess.auth = (app_conf.bot_github_username, app_conf.bot_github_token)
    github = website.platforms.github
    label, repo = app_conf.payday_label, app_conf.payday_repo
    # Fetch the previous payday issue
    path = '/repos/%s/issues' % repo
    params = {'state': 'all', 'labels': label, 'per_page': 1}
    r = github.api_get('', path, params=params, sess=sess).json()
    last_issue = r[0] if r else None
    # Create the next payday issue
    next_issue = {'body': '', 'labels': [label]}
    if last_issue:
        last_issue_payday_id = str(int(last_issue['title'].split()[-1].lstrip('#')))
        if last_issue_payday_id == next_payday_id:
            return  # already created
        assert last_issue_payday_id == str(last_payday.id)
        next_issue['title'] = last_issue['title'].replace(last_issue_payday_id, next_payday_id)
        next_issue['body'] = last_issue['body']
    else:
        next_issue['title'] = "Payday #%s" % next_payday_id
    next_issue = github.api_request('POST', '', '/repos/%s/issues' % repo, json=next_issue, sess=sess).json()
    website.db.run("""
        INSERT INTO paydays
                    (id, public_log, ts_start)
             VALUES (%s, %s, NULL)
    """, (next_payday_id, next_issue['html_url']))


def payday_preexec():  # pragma: no cover
    # Tweak env
    from os import environ
    environ['CACHE_STATIC'] = 'no'
    environ['CLEAN_ASSETS'] = 'no'
    environ['RUN_CRON_JOBS'] = 'no'
    environ['PYTHONPATH'] = website.project_root
    # Write PID file
    pid_file = open(website.env.log_dir+'/payday.pid', 'w')
    pid_file.write(str(os.getpid()))


def exec_payday(log_file):  # pragma: no cover
    # Fork twice, like a traditional unix daemon
    if os.fork():
        return
    if os.fork():
        os.execlp('true', 'true')
    # Fork again and exec
    devnull = open(os.devnull)
    Popen(
        [sys.executable, '-u', 'liberapay/billing/payday.py'],
        stdin=devnull, stdout=log_file, stderr=log_file,
        close_fds=True, cwd=website.project_root, preexec_fn=payday_preexec,
    )
    os.execlp('true', 'true')


def main(override_payday_checks=False):
    from liberapay.billing.transactions import sync_with_mangopay
    from liberapay.main import website

    # https://github.com/liberapay/salon/issues/19#issuecomment-191230689
    from liberapay.billing.payday import Payday

    if not website.env.override_payday_checks and not override_payday_checks:
        # Check that payday hasn't already been run this week
        r = website.db.one("""
            SELECT id
              FROM paydays
             WHERE ts_start >= now() - INTERVAL '6 days'
               AND ts_end >= ts_start
        """)
        assert not r, "payday has already been run this week"

    # Prevent a race condition, by acquiring a DB lock
    conn = website.db.get_connection().__enter__()
    cursor = conn.cursor()
    lock = cursor.one("SELECT pg_try_advisory_lock(1)")
    assert lock, "failed to acquire the payday lock"

    try:
        sync_with_mangopay(website.db)
        Payday.start().run(website.env.log_dir, website.env.keep_payday_logs)
    except KeyboardInterrupt:  # pragma: no cover
        pass
    except Exception as e:  # pragma: no cover
        website.tell_sentry(e, {}, allow_reraise=False)
        raise
    finally:
        conn.close()


if __name__ == '__main__':  # pragma: no cover
    main()
