# Identity: the EKS Pod Identity add-on on the sandbox cluster and one association binding the
# frontend's ServiceAccount (added by the sandbox patch) to orion-bff. The sandbox's pods keep
# automountServiceAccountToken: false; whether Pod Identity works so is Open question 19.

resource "aws_eks_addon" "pod_identity" {
  count         = var.create_pod_identity_addon ? 1 : 0
  cluster_name  = var.eks_cluster_name
  addon_name    = "eks-pod-identity-agent"
  addon_version = var.pod_identity_addon_version
}

# Session tags stay on: orion-bff's trust (iam.tf) admits only the kubernetes-namespace and
# kubernetes-service-account tags of this association, so with them off the pod gets no credentials.
resource "aws_eks_pod_identity_association" "bff" {
  cluster_name         = var.eks_cluster_name
  namespace            = var.kubernetes_namespace
  service_account      = var.frontend_service_account
  role_arn             = aws_iam_role.bff.arn
  disable_session_tags = false
  depends_on           = [aws_eks_addon.pod_identity]
}
