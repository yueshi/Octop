"""Database services — RepoBundle and SharedServices DI container."""

from __future__ import annotations

from dataclasses import dataclass

from octop.config import OctopConfig
from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos.agents import AgentRepo
from octop.infra.db.repos.apps import LightAppRepo
from octop.infra.db.repos.audit import AuditRepo
from octop.infra.db.repos.autonomy_policy import AutonomyPolicyRepo
from octop.infra.db.repos.backends import BackendRepo
from octop.infra.db.repos.boards import LightAppBoardRepo
from octop.infra.db.repos.bridge_connections import BridgeConnectionRepo
from octop.infra.db.repos.care_push import CarePushRepo
from octop.infra.db.repos.channels import ChannelRepo
from octop.infra.db.repos.connectors import ConnectorRepo
from octop.infra.db.repos.cron import CronJobRepo
from octop.infra.db.repos.external_deliveries import ExternalDeliveryRepo
from octop.infra.db.repos.external_events import ExternalEventRepo
from octop.infra.db.repos.external_sources import ExternalSourceRepo
from octop.infra.db.repos.external_subscriptions import ExternalSubscriptionRepo
from octop.infra.db.repos.goal_deliverables import GoalDeliverableRepo
from octop.infra.db.repos.hitl import HitlPendingRepo
from octop.infra.db.repos.invites import InviteRepo
from octop.infra.db.repos.knowledge import KnowledgeRepo
from octop.infra.db.repos.proactive_care_config import ProactiveCareConfigRepo
from octop.infra.db.repos.providers import ProviderRepo
from octop.infra.db.repos.published_experts import PublishedExpertRepo
from octop.infra.db.repos.secrets import SecretRepo
from octop.infra.db.repos.sessions import SessionRepo
from octop.infra.db.repos.settings import SettingsRepo
from octop.infra.db.repos.skill_packages import SkillPackageRepo
from octop.infra.db.repos.sso import SsoRepo
from octop.infra.db.repos.team_decisions import TeamDecisionRepo
from octop.infra.db.repos.team_events import TeamEventRepo
from octop.infra.db.repos.team_goals import TeamGoalRepo
from octop.infra.db.repos.team_runtime import TeamRuntimeRepo
from octop.infra.db.repos.team_task_deps import TeamTaskDepsRepo
from octop.infra.db.repos.team_task_reviews import TeamTaskReviewRepo
from octop.infra.db.repos.team_tasks import TeamTaskRepo
from octop.infra.db.repos.team_waits import TeamWaitRepo
from octop.infra.db.repos.thread_messages import ThreadMessageRepo
from octop.infra.db.repos.threads import ThreadRepo
from octop.infra.db.repos.trajectory_events import TrajectoryEventRepo
from octop.infra.db.repos.usage import UsageRepo
from octop.infra.db.repos.user_policies import UserPolicyRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.repos.voice_providers import VoiceProviderRepo
from octop.infra.utils.paths import PathLayout


@dataclass(frozen=True)
class RepoBundle:
    db: DatabasePool

    user_repo: UserRepo
    user_policy_repo: UserPolicyRepo
    invite_repo: InviteRepo
    agent_repo: AgentRepo
    provider_repo: ProviderRepo
    channel_repo: ChannelRepo
    cron_repo: CronJobRepo
    session_repo: SessionRepo
    thread_repo: ThreadRepo
    thread_message_repo: ThreadMessageRepo
    trajectory_event_repo: TrajectoryEventRepo
    secret_repo: SecretRepo
    audit_repo: AuditRepo
    usage_repo: UsageRepo
    settings_repo: SettingsRepo
    storage_backend_repo: BackendRepo
    bridge_connection_repo: BridgeConnectionRepo
    connector_repo: ConnectorRepo
    skill_package_repo: SkillPackageRepo
    published_expert_repo: PublishedExpertRepo
    knowledge_repo: KnowledgeRepo
    light_app_repo: LightAppRepo
    light_app_board_repo: LightAppBoardRepo
    voice_provider_repo: VoiceProviderRepo
    care_push_repo: CarePushRepo
    proactive_care_config_repo: ProactiveCareConfigRepo
    autonomy_policy_repo: AutonomyPolicyRepo
    sso_repo: SsoRepo
    team_goal_repo: TeamGoalRepo
    goal_deliverable_repo: GoalDeliverableRepo
    team_task_repo: TeamTaskRepo
    team_runtime_repo: TeamRuntimeRepo
    team_event_repo: TeamEventRepo
    team_decision_repo: TeamDecisionRepo
    team_wait_repo: TeamWaitRepo
    team_task_deps_repo: TeamTaskDepsRepo
    team_task_review_repo: TeamTaskReviewRepo
    hitl_repo: HitlPendingRepo
    external_source_repo: ExternalSourceRepo
    external_subscription_repo: ExternalSubscriptionRepo
    external_event_repo: ExternalEventRepo
    external_delivery_repo: ExternalDeliveryRepo

    @classmethod
    def from_pool(cls, db: DatabasePool) -> RepoBundle:
        return cls(
            db=db,
            user_repo=UserRepo(db),
            user_policy_repo=UserPolicyRepo(db),
            invite_repo=InviteRepo(db),
            agent_repo=AgentRepo(db),
            provider_repo=ProviderRepo(db),
            channel_repo=ChannelRepo(db),
            cron_repo=CronJobRepo(db),
            session_repo=SessionRepo(db),
            thread_repo=ThreadRepo(db),
            thread_message_repo=ThreadMessageRepo(db),
            trajectory_event_repo=TrajectoryEventRepo(db),
            secret_repo=SecretRepo(db),
            audit_repo=AuditRepo(db),
            usage_repo=UsageRepo(db),
            settings_repo=SettingsRepo(db),
            storage_backend_repo=BackendRepo(db),
            bridge_connection_repo=BridgeConnectionRepo(db),
            connector_repo=ConnectorRepo(db),
            skill_package_repo=SkillPackageRepo(db),
            published_expert_repo=PublishedExpertRepo(db),
            knowledge_repo=KnowledgeRepo(db),
            light_app_repo=LightAppRepo(db),
            light_app_board_repo=LightAppBoardRepo(db),
            voice_provider_repo=VoiceProviderRepo(db),
            care_push_repo=CarePushRepo(db),
            proactive_care_config_repo=ProactiveCareConfigRepo(db),
            autonomy_policy_repo=AutonomyPolicyRepo(db),
            sso_repo=SsoRepo(db),
            team_goal_repo=TeamGoalRepo(db),
            goal_deliverable_repo=GoalDeliverableRepo(db),
            team_task_repo=TeamTaskRepo(db),
            team_runtime_repo=TeamRuntimeRepo(db),
            team_event_repo=TeamEventRepo(db),
            team_decision_repo=TeamDecisionRepo(db),
            team_wait_repo=TeamWaitRepo(db),
            team_task_deps_repo=TeamTaskDepsRepo(db),
            team_task_review_repo=TeamTaskReviewRepo(db),
            hitl_repo=HitlPendingRepo(db),
            external_source_repo=ExternalSourceRepo(db),
            external_subscription_repo=ExternalSubscriptionRepo(db),
            external_event_repo=ExternalEventRepo(db),
            external_delivery_repo=ExternalDeliveryRepo(db),
        )


@dataclass(frozen=True)
class SharedServices:
    paths: PathLayout
    config: OctopConfig
    repos: RepoBundle

    @property
    def db(self) -> DatabasePool:
        return self.repos.db

    @property
    def user_repo(self) -> UserRepo:
        return self.repos.user_repo

    @property
    def user_policy_repo(self) -> UserPolicyRepo:
        return self.repos.user_policy_repo

    @property
    def invite_repo(self) -> InviteRepo:
        return self.repos.invite_repo

    @property
    def agent_repo(self) -> AgentRepo:
        return self.repos.agent_repo

    @property
    def provider_repo(self) -> ProviderRepo:
        return self.repos.provider_repo

    @property
    def channel_repo(self) -> ChannelRepo:
        return self.repos.channel_repo

    @property
    def cron_repo(self) -> CronJobRepo:
        return self.repos.cron_repo

    @property
    def session_repo(self) -> SessionRepo:
        return self.repos.session_repo

    @property
    def thread_repo(self) -> ThreadRepo:
        return self.repos.thread_repo

    @property
    def thread_message_repo(self) -> ThreadMessageRepo:
        return self.repos.thread_message_repo

    @property
    def trajectory_event_repo(self) -> TrajectoryEventRepo:
        return self.repos.trajectory_event_repo

    @property
    def secret_repo(self) -> SecretRepo:
        return self.repos.secret_repo

    @property
    def audit_repo(self) -> AuditRepo:
        return self.repos.audit_repo

    @property
    def usage_repo(self) -> UsageRepo:
        return self.repos.usage_repo

    @property
    def settings_repo(self) -> SettingsRepo:
        return self.repos.settings_repo

    @property
    def storage_backend_repo(self) -> BackendRepo:
        return self.repos.storage_backend_repo

    @property
    def bridge_connection_repo(self) -> BridgeConnectionRepo:
        return self.repos.bridge_connection_repo

    @property
    def connector_repo(self) -> ConnectorRepo:
        return self.repos.connector_repo

    @property
    def skill_package_repo(self) -> SkillPackageRepo:
        return self.repos.skill_package_repo

    @property
    def published_expert_repo(self) -> PublishedExpertRepo:
        return self.repos.published_expert_repo

    @property
    def knowledge_repo(self) -> KnowledgeRepo:
        return self.repos.knowledge_repo

    @property
    def light_app_repo(self) -> LightAppRepo:
        return self.repos.light_app_repo

    @property
    def light_app_board_repo(self) -> LightAppBoardRepo:
        return self.repos.light_app_board_repo

    @property
    def voice_provider_repo(self) -> VoiceProviderRepo:
        return self.repos.voice_provider_repo

    @property
    def care_push_repo(self) -> CarePushRepo:
        return self.repos.care_push_repo

    @property
    def proactive_care_config_repo(self) -> ProactiveCareConfigRepo:
        return self.repos.proactive_care_config_repo

    @property
    def autonomy_policy_repo(self) -> AutonomyPolicyRepo:
        return self.repos.autonomy_policy_repo

    @property
    def sso_repo(self) -> SsoRepo:
        return self.repos.sso_repo

    @property
    def team_goal_repo(self) -> TeamGoalRepo:
        return self.repos.team_goal_repo

    @property
    def goal_deliverable_repo(self) -> GoalDeliverableRepo:
        return self.repos.goal_deliverable_repo

    @property
    def team_task_repo(self) -> TeamTaskRepo:
        return self.repos.team_task_repo

    @property
    def team_runtime_repo(self) -> TeamRuntimeRepo:
        return self.repos.team_runtime_repo

    @property
    def team_event_repo(self) -> TeamEventRepo:
        return self.repos.team_event_repo

    @property
    def team_decision_repo(self) -> TeamDecisionRepo:
        return self.repos.team_decision_repo

    @property
    def team_wait_repo(self) -> TeamWaitRepo:
        return self.repos.team_wait_repo

    @property
    def team_task_deps_repo(self) -> TeamTaskDepsRepo:
        return self.repos.team_task_deps_repo

    @property
    def team_task_review_repo(self) -> TeamTaskReviewRepo:
        return self.repos.team_task_review_repo

    @property
    def hitl_repo(self) -> HitlPendingRepo:
        return self.repos.hitl_repo

    @property
    def external_source_repo(self) -> ExternalSourceRepo:
        return self.repos.external_source_repo

    @property
    def external_subscription_repo(self) -> ExternalSubscriptionRepo:
        return self.repos.external_subscription_repo

    @property
    def external_event_repo(self) -> ExternalEventRepo:
        return self.repos.external_event_repo

    @property
    def external_delivery_repo(self) -> ExternalDeliveryRepo:
        return self.repos.external_delivery_repo


def build_shared_services(
    *, db: DatabasePool, paths: PathLayout, config: OctopConfig
) -> SharedServices:
    return SharedServices(
        paths=paths,
        config=config,
        repos=RepoBundle.from_pool(db),
    )
