import json
import unittest
from unittest.mock import MagicMock, patch
from fastapi import HTTPException

from app import schema_bootstrap
from app.routers import ticket_insert_external as external


def _configured_channel():
    return {
        "bot_input_fields": [
            "sAction",
            "sRequest",
            "sCRMID",
            "sEmail",
            "sCustomerName",
            "sCustomerType",
            "sIdentificationNumber",
            "sLanguage",
            "sChannel",
            "sState",
            "sContactNumber",
            "sFilename",
            "sOCRStatus",
        ]
    }


def _cidb_scenario_row():
    return {
        "scenario_label": "Cancellation Email Masterbot",
        "hardcoded_json": json.dumps({
            "ScheduleName": "Cancellation Email Masterbot",
            "PlayID": 105,
            "PlayOrder": 1,
            "GroupID": "9-1784599526",
            "CompanyID": 1,
            "BotID": 118,
        }),
        "channels_json": json.dumps({"Email": _configured_channel()}),
    }


class CidbExternalTicketTests(unittest.TestCase):
    def setUp(self):
        self.executed = []
        self.patches = [
            patch.object(external, "mysql_fetch_all", return_value=[_cidb_scenario_row()]),
            patch.object(
                external,
                "_select_runner",
                return_value=(
                    {
                        "RunnerName": "test-runner",
                        "RunnerID": "runner-1",
                        "ControlRoomIP": "127.0.0.1",
                    },
                    False,
                ),
            ),
            patch.object(
                external,
                "mysql_execute",
                side_effect=lambda sql, params: self.executed.append((sql, params)),
            ),
            patch.object(external, "write_audit"),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    def test_cidb_scenario_is_seeded_without_overwriting_existing_config(self):
        connection = MagicMock()
        cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value

        with patch.object(schema_bootstrap, "mysql_conn", return_value=connection):
            schema_bootstrap._seed_ticket_scenarios()

        cidb_insert = next(
            call for call in cursor.execute.call_args_list
            if call.args[1][0] == "CIDB"
        )
        self.assertIn("INSERT IGNORE", cidb_insert.args[0])
        self.assertEqual(cidb_insert.args[1][:3], (
            "CIDB",
            "cidb_masterbot",
            "Cancellation Email Masterbot",
        ))
        hardcoded = json.loads(cidb_insert.args[1][3])
        channel = json.loads(cidb_insert.args[1][4])["Email"]
        self.assertEqual(
            {
                "ScheduleName": hardcoded["ScheduleName"],
                "PlayOrder": hardcoded["PlayOrder"],
                "PlayID": hardcoded["PlayID"],
                "GroupID": hardcoded["GroupID"],
                "CompanyID": hardcoded["CompanyID"],
                "BotID": hardcoded["BotID"],
            },
            {
                "ScheduleName": "Cancellation Email Masterbot",
                "PlayOrder": 1,
                "PlayID": 105,
                "GroupID": "9-1784599526",
                "CompanyID": 1,
                "BotID": 118,
            },
        )
        self.assertIn("sRequst", channel["bot_input_fields"])
        self.assertIn("sOCRStatus", channel["bot_input_fields"])

    def test_attachment_request_builds_allowlisted_bot_input(self):
        payload = external.ExternalTicketRequest(
            company="CIDB",
            scenario_key="cidb_masterbot",
            channel="Email",
            fields={
                "sRequest": "Pending Customer Attachment",
                "sCRMID": "CRM-123",
                "sEmail": "customer@example.test",
                "sCustomerName": "Sample Customer",
                "sLanguage": "en",
                "sState": "Sarawak",
                "sContactNumber": "0123456789",
                "sUnapprovedField": "must not enter Bot_Input",
            },
        )

        response = external.external_ticket_insert(payload, key_hint="unit-test")

        self.assertEqual(response.status, "inserted")
        self.assertEqual(self.executed[0][1][1:7], (
            "Cancellation Email Masterbot",
            1,
            105,
            "9-1784599526",
            1,
            118,
        ))
        self.assertEqual(response.bot_input, {
            "sRequest": "Pending Customer Attachment",
            "sCRMID": "CRM-123",
            "sEmail": "customer@example.test",
            "sCustomerName": "Sample Customer",
            "sLanguage": "en",
            "sChannel": "Email",
            "sState": "Sarawak",
            "sContactNumber": "0123456789",
        })
        self.assertEqual(len(self.executed), 1)

    def test_cancellation_requires_identity_and_ocr_context(self):
        payload = external.ExternalTicketRequest(
            company="CIDB",
            scenario_key="cidb_masterbot",
            channel="Email",
            fields={
                "sAction": "Email Cancellation",
                "sCRMID": "CRM-123",
                "sEmail": "customer@example.test",
                "sCustomerName": "Sample Customer",
                "sCustomerType": "Individual",
                "sIdentificationNumber": "900101101234",
                "sLanguage": "ms",
                "sOCRStatus": "MANUAL_REVIEW_APPROVED",
                "sFilename": "signed-form.pdf",
            },
        )

        response = external.external_ticket_insert(payload, key_hint="unit-test")

        self.assertEqual(response.bot_input["sAction"], "Email Cancellation")
        self.assertEqual(response.bot_input["sIdentificationNumber"], "900101101234")
        self.assertEqual(response.bot_input["sOCRStatus"], "MANUAL_REVIEW_APPROVED")
        self.assertEqual(len(self.executed), 1)

    def test_request_variable_uses_configured_bot_spelling(self):
        scenario = _cidb_scenario_row()
        hardcoded = {
            "ScheduleName": "Cancellation Email Masterbot",
            "PlayID": 105,
            "PlayOrder": 1,
            "GroupID": "9-1784599526",
            "CompanyID": 1,
            "BotID": 118,
        }
        bot_fields = [
            field for field in _configured_channel()["bot_input_fields"]
            if field != "sRequest"
        ]
        bot_fields.insert(0, "sRequst")
        with patch.object(
            external,
            "_load_cidb_scenario",
            return_value=(scenario, hardcoded, {"bot_input_fields": bot_fields}),
        ):
            payload = external.ExternalTicketRequest(
                company="CIDB",
                scenario_key="cidb_masterbot",
                channel="Email",
                fields={
                    "sRequst": "Pending Customer Attachment",
                    "sCRMID": "CRM-123",
                    "sEmail": "customer@example.test",
                    "sCustomerName": "Sample Customer",
                    "sLanguage": "en",
                },
            )
            response = external.external_ticket_insert(payload, key_hint="unit-test")

        self.assertEqual(response.bot_input["sRequst"], "Pending Customer Attachment")
        self.assertNotIn("sRequest", response.bot_input)

    def test_rejects_mixed_actions_and_missing_cancellation_identity(self):
        mixed = external.ExternalTicketRequest(
            company="CIDB",
            scenario_key="cidb_masterbot",
            channel="Email",
            fields={
                "sRequest": "Pending Customer Attachment",
                "sAction": "Email Cancellation",
                "sCRMID": "CRM-123",
                "sEmail": "customer@example.test",
                "sCustomerName": "Sample Customer",
                "sLanguage": "en",
            },
        )
        with self.assertRaises(HTTPException) as error:
            external.external_ticket_insert(mixed, key_hint="unit-test")
        self.assertEqual(error.exception.status_code, 400)

        incomplete = external.ExternalTicketRequest(
            company="CIDB",
            scenario_key="cidb_masterbot",
            channel="Email",
            fields={
                "sAction": "Email Cancellation",
                "sCRMID": "CRM-123",
                "sEmail": "customer@example.test",
                "sCustomerName": "Sample Customer",
                "sCustomerType": "Individual",
                "sLanguage": "en",
                "sOCRStatus": "MANUAL_REVIEW_APPROVED",
            },
        )
        with self.assertRaises(HTTPException) as error:
            external.external_ticket_insert(incomplete, key_hint="unit-test")
        self.assertEqual(error.exception.status_code, 400)
        self.assertEqual(self.executed, [])

    def test_rejects_cancellation_without_manual_ocr_approval(self):
        payload = external.ExternalTicketRequest(
            company="CIDB",
            scenario_key="cidb_masterbot",
            channel="Email",
            fields={
                "sAction": "Email Cancellation",
                "sCRMID": "CRM-123",
                "sEmail": "customer@example.test",
                "sCustomerName": "Sample Customer",
                "sCustomerType": "Individual",
                "sIdentificationNumber": "900101101234",
                "sLanguage": "en",
                "sOCRStatus": "VERIFIED",
            },
        )

        with self.assertRaises(HTTPException) as error:
            external.external_ticket_insert(payload, key_hint="unit-test")

        self.assertEqual(error.exception.status_code, 400)
        self.assertEqual(self.executed, [])

    def test_refuses_unconfigured_bot_input_contract(self):
        scenario = _cidb_scenario_row()
        hardcoded = {
            "ScheduleName": "CIDB Masterbot",
            "PlayID": 9001,
            "PlayOrder": 1,
            "GroupID": "cidb-group",
            "CompanyID": 1,
            "BotID": 9002,
        }
        with patch.object(
            external,
            "_load_cidb_scenario",
            return_value=(scenario, hardcoded, {"bot_input_fields": ["sCRMID", "sEmail"]}),
        ):
            payload = external.ExternalTicketRequest(
                company="CIDB",
                scenario_key="cidb_masterbot",
                channel="Email",
                fields={
                    "sRequest": "Pending Customer Attachment",
                    "sCRMID": "CRM-123",
                    "sEmail": "customer@example.test",
                    "sCustomerName": "Sample Customer",
                    "sLanguage": "en",
                },
            )
            with self.assertRaises(HTTPException) as error:
                external.external_ticket_insert(payload, key_hint="unit-test")

        self.assertEqual(error.exception.status_code, 503)
        self.assertEqual(self.executed, [])


if __name__ == "__main__":
    unittest.main()
