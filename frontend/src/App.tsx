import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { AuthProvider } from './hooks/useAuth'
import ProtectedRoute from './components/ProtectedRoute'
import LoginPage from './pages/LoginPage'
import RegisterPage from './pages/RegisterPage'
import DashboardPage from './pages/DashboardPage'
import JobDetailPage from './pages/JobDetailPage'
import AdminPage from './pages/AdminPage'
import AdminTenantDetailPage from './pages/AdminTenantDetailPage'
import SagasPage from './pages/SagasPage'
import SagaNewPage from './pages/SagaNewPage'
import SagaDetailPage from './pages/SagaDetailPage'

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/register" element={<RegisterPage />} />

          <Route
            path="/jobs"
            element={
              <ProtectedRoute>
                <DashboardPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/jobs/:id"
            element={
              <ProtectedRoute>
                <JobDetailPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/sagas"
            element={
              <ProtectedRoute>
                <SagasPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/sagas/new"
            element={
              <ProtectedRoute>
                <SagaNewPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/sagas/:id"
            element={
              <ProtectedRoute>
                <SagaDetailPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/admin"
            element={
              <ProtectedRoute requiredRole="support">
                <AdminPage />
              </ProtectedRoute>
            }
          />
          <Route
            path="/admin/tenants/:id"
            element={
              <ProtectedRoute requiredRole="admin">
                <AdminTenantDetailPage />
              </ProtectedRoute>
            }
          />

          <Route path="/" element={<Navigate to="/jobs" replace />} />
          <Route path="*" element={<Navigate to="/jobs" replace />} />
        </Routes>
      </AuthProvider>
    </BrowserRouter>
  )
}
