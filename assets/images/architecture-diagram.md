# Architecture Diagrams

## 1. Authentication and Credential Flow

This diagram shows the complete process for obtaining temporary AWS credentials and using them to access Amazon Bedrock.

```mermaid
sequenceDiagram
    participant Dev as Developer
    participant CLI as Claude Code CLI
    participant Cache as Local Credential Cache
    participant Browser as Web Browser
    participant OIDC as OIDC Provider<br/>(Okta/Azure AD/Google)
    participant Cognito as AWS Cognito<br/>Identity Pool
    participant STS as AWS STS
    participant Bedrock as Amazon Bedrock

    Dev->>CLI: aws bedrock-runtime invoke-model
    CLI->>Cache: Check for valid credentials
    
    alt Credentials not cached or expired
        Cache-->>CLI: No valid credentials
        CLI->>Browser: Open auth URL (localhost:8400)
        Browser->>OIDC: Redirect to OIDC login
        Dev->>OIDC: Enter credentials + MFA
        OIDC->>Browser: Return OIDC token
        Browser->>CLI: Return auth code
        CLI->>OIDC: Exchange code for ID token
        OIDC->>CLI: Return ID token
        CLI->>Cognito: Exchange OIDC token
        Cognito->>Cognito: Validate OIDC token
        Cognito->>STS: AssumeRoleWithWebIdentity
        STS->>Cognito: Return temporary credentials
        Cognito->>CLI: Return AWS credentials<br/>(AccessKey, SecretKey, SessionToken)
        CLI->>Cache: Store credentials (8 hours)
    else Credentials cached and valid
        Cache-->>CLI: Return cached credentials
    end
    
    CLI->>Bedrock: Invoke model with credentials
    Bedrock->>Bedrock: Validate IAM permissions
    Bedrock->>CLI: Return AI response
    CLI->>Dev: Display response

    Note over Dev,Bedrock: All credentials are temporary (max 8 hours)<br/>No long-lived API keys are stored
```

## 2. OpenTelemetry Monitoring Architecture

This diagram illustrates the optional monitoring setup using OpenTelemetry collector on ECS Fargate.

```mermaid
flowchart TB
    subgraph "Developer Machines"
        CLI1[Claude Code CLI 1]
        CLI2[Claude Code CLI 2]
        CLI3[Claude Code CLI N]
    end

    subgraph "AWS Account"
        subgraph "ECS Fargate"
            Collector[OpenTelemetry Collector<br/>Container]
        end
        
        subgraph "CloudWatch"
            Metrics[CloudWatch Metrics]
            Logs[CloudWatch Logs]
            Dashboard[CloudWatch Dashboard]
            Alarms[CloudWatch Alarms]
        end
        
        subgraph "Storage"
            S3[S3 Bucket<br/>Log Archive]
        end
    end

    CLI1 -->|OTLP/gRPC<br/>Port 4317| Collector
    CLI2 -->|OTLP/gRPC<br/>Port 4317| Collector
    CLI3 -->|OTLP/gRPC<br/>Port 4317| Collector

    Collector -->|Export Metrics| Metrics
    Collector -->|Export Logs| Logs
    Collector -->|Export Traces| Logs

    Metrics --> Dashboard
    Metrics --> Alarms
    Logs --> Dashboard
    Logs -->|Archive| S3

    Alarms -->|Notify| SNS[SNS Topic<br/>Optional Alerts]

    Note1[Authentication Metrics:<br/>- Total authentications<br/>- Failed authentications<br/>- Authentication latency<br/>- Active users]
    
    Note2[Bedrock Usage Metrics:<br/>- API calls by model<br/>- Token usage<br/>- Error rates<br/>- Response times]

    style Collector fill:#f9f,stroke:#333,stroke-width:2px
    style Dashboard fill:#9f9,stroke:#333,stroke-width:2px
    style Note1 fill:#ffd,stroke:#333,stroke-width:1px,stroke-dasharray: 5 5
    style Note2 fill:#ffd,stroke:#333,stroke-width:1px,stroke-dasharray: 5 5
```

## AWS Architecture Icon Requirements

For official AWS architecture diagrams (PNG format):
- Use latest AWS Architecture Icons Toolkit (light background, released 04.28.2023)
- Service icons ≥0.4"×0.4", grouping icons ≥0.3"×0.3"
- All icons must have labels at bottom, Arial 9-12pt in black
- "AWS" or "Amazon" appears in same line as first word of service
- Solid black arrows (1.25pt width), no diagonal lines
- No cropping, flipping, or shape modifications allowed

## Key Architecture Components

1. **Developer Workstation**: Runs Claude Code CLI with local credential caching
2. **OIDC Provider**: Enterprise identity provider (Okta, Azure AD, Google Workspace)
3. **Amazon Cognito Identity Pool**: Validates OIDC tokens and manages identity federation
4. **AWS STS**: Issues temporary credentials via AssumeRoleWithWebIdentity
5. **Amazon Bedrock**: Target AI service accessed with temporary credentials
6. **AWS CloudTrail**: Captures all authentication and API access events
7. **Amazon CloudWatch**: Optional monitoring dashboard and alerting
8. **Amazon ECS Fargate**: Hosts OpenTelemetry collector for centralized telemetry